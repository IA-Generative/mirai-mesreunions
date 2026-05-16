"""In-memory store des jobs de génération de brief (Lot 2).

Conserve l'état de progression d'un job ``POST /api/preparations`` en mémoire
process (dict + lock). Pas de migration SQL nécessaire : la persistance finale
est portée par la table ``preparations`` (le brief généré y est écrit via
``prep_service.create_preparation`` une fois la phase ``generating_llm``
terminée).

Note : single-pod assumption pour le polling (le pod qui a démarré le job est
celui qui répond au polling). En cas de multi-replica + service IP non sticky,
le client retomberait sur un pod qui ne connaît pas le job → réponse ``unknown``
gérée côté front (l'UX reste correcte : on bascule en fallback "génération en
cours…" sans stepper détaillé). Mydevices-web tourne en 1 replica prod-bêta
donc OK pour le périmètre PR-1.

Phases possibles :
  - ``queued``           : créé, worker pas encore démarré
  - ``init``             : worker démarré, configs validées
  - ``test_drive``       : exchange refresh→access en cours
  - ``listing_docs``     : list_children() en cours
  - ``reading_doc``      : extraction d'un document (avec ``current_doc`` + ``docs_processed``/``docs_total``)
  - ``generating_llm``   : appel LLM en cours
  - ``persisting``       : insertion DB + sync Drive
  - ``extracting_glossary`` : extraction termes
  - ``done``             : terminé OK, ``preparation_id`` rempli
  - ``failed``           : échec, ``error`` rempli
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

_LOCK = threading.RLock()
_JOBS: dict[str, dict] = {}
_TTL_SECONDS = 3600  # 1h


def _now() -> float:
    return time.time()


def _gc_locked() -> None:
    """Purge les jobs terminés > TTL (appelé sous _LOCK)."""
    threshold = _now() - _TTL_SECONDS
    stale = [
        jid for jid, j in _JOBS.items()
        if j.get("finished_at") and j["finished_at"] < threshold
    ]
    for jid in stale:
        _JOBS.pop(jid, None)


def create_job(user_sub: str) -> str:
    """Crée un job ``queued`` et retourne son id (uuid4 hex)."""
    job_id = uuid.uuid4().hex
    with _LOCK:
        _gc_locked()
        _JOBS[job_id] = {
            "id": job_id,
            "user_sub": user_sub or "",
            "phase": "queued",
            "current_doc": None,
            "docs_processed": 0,
            "docs_total": 0,
            "preparation_id": None,
            "error": None,
            "started_at": _now(),
            "finished_at": None,
        }
    return job_id


def update_job(job_id: str, **fields) -> None:
    """Met à jour les champs d'un job (best-effort, silencieux si inconnu)."""
    if not job_id:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        for k, v in fields.items():
            job[k] = v


def mark_failed(job_id: str, error: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["phase"] = "failed"
        job["error"] = str(error)[:500]
        job["finished_at"] = _now()


def mark_done(job_id: str, preparation_id: Optional[str]) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["phase"] = "done"
        job["preparation_id"] = preparation_id
        job["finished_at"] = _now()


def get_job(job_id: str, user_sub: str) -> Optional[dict]:
    """Retourne une copie shallow du job (ou None) si user_sub matche."""
    if not job_id:
        return None
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        # Isolation : on ne révèle que ses propres jobs.
        if user_sub and job.get("user_sub") and job["user_sub"] != user_sub:
            return None
        return dict(job)
