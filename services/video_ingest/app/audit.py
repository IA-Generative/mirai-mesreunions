"""Journal d'audit (Q5 / migration 021).

API minimaliste : `log(conn, action, user_sub, **details)`. Si
l'écriture échoue, on log au logger Python mais on **ne lève pas** :
l'audit ne doit jamais casser le flux fonctionnel (compromis volontaire
documenté).
"""

from __future__ import annotations

import logging

from psycopg2.extras import Json

log = logging.getLogger(__name__)


def log_event(
    conn,
    *,
    action: str,
    user_sub: str,
    url: str | None = None,
    video_source_id: int | None = None,
    reused: bool | None = None,
    job_id: int | None = None,
    context: str | None = None,
    context_id: str | None = None,
    details: dict | None = None,
) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO video_ingest_audit (
                    action, user_sub, url, video_source_id, reused, job_id,
                    context, context_id, details_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (action, user_sub, url, video_source_id, reused, job_id,
                 context, context_id, Json(details or {})),
            )
    except Exception:
        log.exception("audit log failed (action=%s user=%s) — non bloquant",
                      action, user_sub)
