"""Connexion BDD — psycopg2 directe, **sans `libs.shared`** (cf. D14).

Le service tient son propre pool. La chaîne de connexion vient de la
variable d'env `VIDEO_INGEST_DATABASE_URL` (forme `postgresql://…`).

Pourquoi pas SQLAlchemy ORM ?
- 4 tables, requêtes simples, raw SQL plus transparent.
- Élimine une dépendance lourde au moment de l'extraction du composant.
- `LISTEN/NOTIFY` natif côté psycopg2.

Pourquoi pas psycopg 3 ?
- Le reste du repo monorepo utilise psycopg2 ; le runtime image est
  déjà testé avec. La migration vers psycopg 3 est un chantier
  séparé, prévu lors de l'extraction du service dans son propre repo.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extensions
import psycopg2.pool


_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _build_pool() -> psycopg2.pool.ThreadedConnectionPool:
    dsn = os.environ.get("VIDEO_INGEST_DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "VIDEO_INGEST_DATABASE_URL n'est pas défini. "
            "Format attendu : postgresql://user:pass@host:port/db"
        )
    minconn = int(os.environ.get("VIDEO_INGEST_DB_MIN_CONN", "1"))
    maxconn = int(os.environ.get("VIDEO_INGEST_DB_MAX_CONN", "8"))
    return psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, dsn)


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = _build_pool()
    return _pool


@contextmanager
def connection(*, autocommit: bool = False) -> Iterator[psycopg2.extensions.connection]:
    """Connexion empruntée au pool, rendue à la sortie.

    `autocommit=True` est nécessaire pour `LISTEN/NOTIFY` (sinon les
    NOTIFY sont retenus en transaction).
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = autocommit
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def cursor(*, autocommit: bool = False) -> Iterator[psycopg2.extensions.cursor]:
    """Curseur prêt à l'emploi (rollback auto sur exception)."""
    with connection(autocommit=autocommit) as conn:
        with conn.cursor() as cur:
            yield cur


def reset_pool_for_tests() -> None:
    """Hook tests : force la reconstruction du pool au prochain appel."""
    global _pool
    if _pool is not None:
        _pool.closeall()
    _pool = None
