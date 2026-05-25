"""File de jobs Postgres-native (D13).

`claim_next` : SELECT … FOR UPDATE SKIP LOCKED + UPDATE lease.
`complete` / `fail` : transitions terminales.
`extend_lease` : heartbeat appelé par le worker en cours de traitement.
`reset_orphans` : watchdog (jobs `running` dont `lease_until` est dépassé).

Toutes les fonctions prennent une `connection` psycopg2 en argument
(injection) pour permettre les tests sans pool global.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Job:
    id: int
    url: str
    user_sub: str
    context: str | None
    context_id: str | None
    language_pref: str | None
    force_audio: bool
    attempts: int


def enqueue(
    conn,
    *,
    url: str,
    user_sub: str,
    context: str | None = None,
    context_id: str | None = None,
    language_pref: str | None = None,
    force_audio: bool = False,
) -> int:
    """Insère un job pending et notifie les workers via `LISTEN/NOTIFY`."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO video_ingest_jobs (
                url, user_sub, context, context_id, language_pref, force_audio
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (url, user_sub, context, context_id, language_pref, force_audio),
        )
        job_id = cur.fetchone()[0]
        cur.execute("NOTIFY video_ingest_jobs, %s", (str(job_id),))
    return job_id


def claim_next(conn, *, claimed_by: str, lease_seconds: int = 90) -> Job | None:
    """Prend le prochain job disponible (pending OU running expiré).

    `SKIP LOCKED` permet à N workers de tirer en parallèle sans collision.
    Transactionnel : si le worker meurt entre claim et heartbeat, le job
    reste verrouillé jusqu'à `lease_until` puis sera repris par le
    watchdog.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH next_job AS (
                SELECT id
                  FROM video_ingest_jobs
                 WHERE status = 'pending'
                    OR (status = 'running' AND lease_until < NOW())
                 ORDER BY created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE video_ingest_jobs j
               SET status      = 'running',
                   claimed_by  = %s,
                   lease_until = NOW() + (%s || ' seconds')::INTERVAL,
                   attempts    = j.attempts + 1,
                   updated_at  = NOW()
              FROM next_job
             WHERE j.id = next_job.id
            RETURNING j.id, j.url, j.user_sub, j.context, j.context_id,
                      j.language_pref, j.force_audio, j.attempts
            """,
            (claimed_by, str(lease_seconds)),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return Job(
        id=row[0], url=row[1], user_sub=row[2], context=row[3],
        context_id=row[4], language_pref=row[5], force_audio=row[6],
        attempts=row[7],
    )


def extend_lease(conn, job_id: int, *, lease_seconds: int = 90) -> bool:
    """Heartbeat — relance le lease. Renvoie False si le job n'est plus
    en `running` (a été failed/done en parallèle, ou repris par un autre)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE video_ingest_jobs
               SET lease_until = NOW() + (%s || ' seconds')::INTERVAL,
                   updated_at  = NOW()
             WHERE id = %s AND status = 'running'
            """,
            (str(lease_seconds), job_id),
        )
        return cur.rowcount > 0


def complete(
    conn,
    job_id: int,
    *,
    video_source_id: int,
    reused: bool,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE video_ingest_jobs
               SET status          = 'done',
                   video_source_id = %s,
                   reused          = %s,
                   completed_at    = NOW(),
                   updated_at      = NOW(),
                   error_message   = NULL,
                   lease_until     = NULL
             WHERE id = %s
            """,
            (video_source_id, reused, job_id),
        )


def fail(conn, job_id: int, *, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE video_ingest_jobs
               SET status        = 'failed',
                   error_message = %s,
                   completed_at  = NOW(),
                   updated_at    = NOW(),
                   lease_until   = NULL
             WHERE id = %s
            """,
            (error[:2000] if error else None, job_id),
        )


def reset_orphans(conn) -> int:
    """Watchdog : repasse en `pending` les jobs `running` dont le lease
    est expiré (worker mort). Renvoie le nombre de jobs réinitialisés.

    À appeler depuis un thread daemon dans chaque worker (pattern
    aligné sur `services/dmz-to-internal-bridge/app/watchdog.py`).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE video_ingest_jobs
               SET status     = 'pending',
                   claimed_by = NULL,
                   updated_at = NOW()
             WHERE status = 'running'
               AND lease_until < NOW()
            """
        )
        return cur.rowcount
