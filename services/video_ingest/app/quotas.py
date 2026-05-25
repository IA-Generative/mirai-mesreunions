"""Quotas anti-abus (Q4).

V1 simple : N imports max par utilisateur sur fenêtre glissante 24h.
Compté sur `video_ingest_jobs.created_at` (donc HIT cache **ne consomme
pas** de quota — c'est volontaire, le coût réel est sur le MISS).

Config via env :
  - `VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY` (défaut 50, 0 = désactivé)
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class QuotaExceeded(Exception):
    def __init__(self, limit: int, current: int):
        super().__init__(f"Quota dépassé : {current}/{limit} imports sur 24h")
        self.limit = limit
        self.current = current


def check_import_quota(conn, *, user_sub: str) -> None:
    """Lève `QuotaExceeded` si le user a atteint son plafond 24h."""
    limit = int(os.environ.get("VIDEO_INGEST_QUOTA_IMPORTS_PER_DAY", "50"))
    if limit <= 0:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
              FROM video_ingest_jobs
             WHERE user_sub = %s
               AND created_at > NOW() - INTERVAL '24 hours'
            """,
            (user_sub,),
        )
        current = cur.fetchone()[0]
    if current >= limit:
        raise QuotaExceeded(limit=limit, current=current)
