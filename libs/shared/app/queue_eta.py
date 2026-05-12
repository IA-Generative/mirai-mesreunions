"""Calcul d'ETA pour la file Kevent (gateway Mirai).

Pure-function. Lit une réponse ``GET /jobs`` (cf kevent_client.list_jobs) et
résume la situation côté file d'attente pour l'utilisateur :

- combien de jobs sont en attente / en traitement
- où se situe le job courant (1-based) si fourni
- ETA estimé, basé sur un temps moyen de traitement constant (les jobs
  ``completed`` étant purgés côté gateway, on n'a pas d'historique réel)

Override possible du temps moyen via env :
  KEVENT_QUEUE_AVG_PROCESS_S = 8.0 (défaut)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


DEFAULT_AVG_S = float(os.getenv("KEVENT_QUEUE_AVG_PROCESS_S", "8.0"))
STALE_AFTER_SECONDS = 30


@dataclass
class QueueSummary:
    pending_total: int             # nombre de jobs pending dans le listing
    processing_total: int          # nombre de jobs processing
    your_position: Optional[int]   # 1-based ; None si own job pas trouvé
    eta_seconds: Optional[int]     # arrondi 10s ; None si non estimable
    throughput_per_min: Optional[float]
    stale: bool                    # max(updated_at) > 30s avant now
    fetched_at: str                # ISO8601 UTC


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00") if isinstance(raw, str) else ""
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def compute_queue_summary(
    payload: dict,
    own_job_id: Optional[str] = None,
    now: Optional[datetime] = None,
    avg_process_s: float = DEFAULT_AVG_S,
) -> QueueSummary:
    """Synthétise un ``QueueSummary`` à partir de la réponse ``/jobs`` brute.

    Args:
        payload: réponse JSON parsée de la gateway (``{jobs: [...], ...}``).
        own_job_id: si fourni, on cherche ce job dans le listing pour
            extraire sa position et estimer son ETA.
        now: ``datetime`` UTC. Si None, utilise ``datetime.now(timezone.utc)``.
        avg_process_s: durée moyenne supposée d'un job. Sert au calcul ETA.

    Returns:
        Un ``QueueSummary`` ; pas de levée d'exception même sur payload
        mal formé (champs neutres).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    fetched_at = now.astimezone(timezone.utc).isoformat()

    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        jobs = []

    pending = [j for j in jobs if (j or {}).get("status") == "pending"]
    processing = [j for j in jobs if (j or {}).get("status") == "processing"]

    # Cherche notre job dans la liste pending pour récupérer sa position
    # 1-based déjà calculée par la gateway. Si absent (déjà picked up,
    # processing, ou completed et purgé), your_position = None.
    your_position: Optional[int] = None
    if own_job_id:
        for j in pending:
            if (j or {}).get("job_id") == own_job_id:
                pos = (j or {}).get("queue_position")
                if isinstance(pos, int) and pos >= 1:
                    your_position = pos
                break

    # ETA : pour notre job, on multiplie sa position par le temps moyen.
    # Pas d'estimation si on ne se trouve pas dans le listing.
    eta_seconds: Optional[int] = None
    if your_position is not None and avg_process_s > 0:
        raw = your_position * avg_process_s
        # Arrondi au 10s le plus proche
        eta_seconds = int(round(raw / 10) * 10)

    throughput_per_min: Optional[float] = None
    if avg_process_s > 0:
        throughput_per_min = round(60.0 / avg_process_s, 2)

    # stale : si AUCUN job pending/processing dans la liste a un updated_at
    # récent (< 30s), on considère la donnée potentiellement stale. Si la
    # liste est vide, on ne marque pas stale (juste vide = legitime).
    stale = False
    interesting = pending + processing
    if interesting:
        max_dt: Optional[datetime] = None
        for j in interesting:
            dt = _parse_dt((j or {}).get("updated_at"))
            if dt and (max_dt is None or dt > max_dt):
                max_dt = dt
        if max_dt is not None:
            stale = (now - max_dt) > timedelta(seconds=STALE_AFTER_SECONDS)

    return QueueSummary(
        pending_total=len(pending),
        processing_total=len(processing),
        your_position=your_position,
        eta_seconds=eta_seconds,
        throughput_per_min=throughput_per_min,
        stale=stale,
        fetched_at=fetched_at,
    )
