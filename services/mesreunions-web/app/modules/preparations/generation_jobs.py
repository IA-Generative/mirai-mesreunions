"""Store partagé multi-pod des jobs de génération de brief.

Backed par postgres-external (table ``preparation_generation_jobs``,
migration 004) — remplace l'ancien in-memory dict qui ne survivait pas
à un multi-replica + sessionAffinity flakey (le polling tombait sur un
pod différent du worker → 404 "Job introuvable").

API publique inchangée — drop-in replacement pour le code appelant :
  - ``create_job(user_sub) -> job_id``
  - ``update_job(job_id, **fields) -> None``
  - ``mark_failed(job_id, error) -> None``
  - ``mark_done(job_id, preparation_id) -> None``
  - ``get_job(job_id, user_sub) -> dict | None``

Phases possibles (cf docstring originale + ORM model) :
  queued | init | test_drive | listing_docs | reading_doc |
  generating_llm | persisting | extracting_glossary | done | failed
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete

from libs.shared.app.models import PreparationGenerationJob

from ...runtime import session_scope

logger = logging.getLogger(__name__)

# Champs ORM qu'on accepte en update — sécurise contre les écritures
# arbitraires venant des appelants (typo dans update_job(**fields)).
_UPDATABLE_FIELDS = {
    "phase", "current_doc", "docs_processed", "docs_total",
    "preparation_id", "error", "finished_at",
}

# TTL applicatif : on purge les jobs terminés > 1h à chaque create_job
# (GC opportuniste, pas de thread dédié — fréquence d'appel >> rythme
# d'accumulation).
_TTL_SECONDS = 3600


def _gc(db) -> None:
    """Purge les jobs terminés depuis plus de TTL. Best-effort."""
    try:
        threshold = datetime.now(timezone.utc) - timedelta(seconds=_TTL_SECONDS)
        db.execute(
            delete(PreparationGenerationJob)
            .where(PreparationGenerationJob.finished_at.isnot(None))
            .where(PreparationGenerationJob.finished_at < threshold)
        )
        db.commit()
    except Exception:
        logger.exception("generation_jobs gc failed (non-fatal)")
        try:
            db.rollback()
        except Exception:
            pass


def _row_to_dict(row: PreparationGenerationJob) -> dict:
    return {
        "id": row.id,
        "user_sub": row.user_sub or "",
        "phase": row.phase or "queued",
        "current_doc": row.current_doc,
        "docs_processed": row.docs_processed or 0,
        "docs_total": row.docs_total or 0,
        "preparation_id": row.preparation_id,
        "error": row.error,
        # Compat : l'ancien store utilisait des timestamps float (time.time()).
        # Le front ne lit pas ces champs aujourd'hui — on garde les
        # datetimes en isoformat pour rester lisibles si jamais consommés.
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def create_job(user_sub: str) -> str:
    """Crée un job ``queued`` et retourne son id (uuid4 hex)."""
    job_id = uuid.uuid4().hex
    db = session_scope()
    try:
        _gc(db)
        row = PreparationGenerationJob(
            id=job_id,
            user_sub=user_sub or "",
            phase="queued",
            docs_processed=0,
            docs_total=0,
            started_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        return job_id
    except Exception:
        logger.exception("generation_jobs.create_job failed for user_sub=%s", user_sub)
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()


def update_job(job_id: str, **fields) -> None:
    """Met à jour les champs d'un job. Best-effort silencieux si inconnu."""
    if not job_id:
        return
    clean = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not clean:
        return
    db = session_scope()
    try:
        row = (
            db.query(PreparationGenerationJob)
            .filter(PreparationGenerationJob.id == job_id)
            .one_or_none()
        )
        if row is None:
            return
        for k, v in clean.items():
            setattr(row, k, v)
        db.commit()
    except Exception:
        logger.exception("generation_jobs.update_job failed job=%s", job_id)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def mark_failed(job_id: str, error: str) -> None:
    """Marque un job comme failed avec son message d'erreur (capped 500 chars)."""
    update_job(
        job_id,
        phase="failed",
        error=str(error)[:500],
        finished_at=datetime.now(timezone.utc),
    )


def mark_done(job_id: str, preparation_id: Optional[str]) -> None:
    """Marque un job comme terminé avec succès."""
    update_job(
        job_id,
        phase="done",
        preparation_id=preparation_id,
        finished_at=datetime.now(timezone.utc),
    )


def get_job(job_id: str, user_sub: str) -> Optional[dict]:
    """Retourne une vue dict du job (ou None) si user_sub matche.

    Isolation : on ne révèle pas les jobs d'un autre user, même par
    accident. Le check est inclusif (user_sub vide = pas de filtre, comme
    l'ancien comportement).
    """
    if not job_id:
        return None
    db = session_scope()
    try:
        row = (
            db.query(PreparationGenerationJob)
            .filter(PreparationGenerationJob.id == job_id)
            .one_or_none()
        )
        if row is None:
            return None
        if user_sub and row.user_sub and row.user_sub != user_sub:
            return None
        return _row_to_dict(row)
    except Exception:
        logger.exception("generation_jobs.get_job failed job=%s", job_id)
        return None
    finally:
        db.close()
