"""Serveur MCP exposant les 5 outils V1 (cf. spec §4.3).

Transport `streamable-http` sur le port 8001 (par convention, l'API REST
écoute sur 8000 — les deux peuvent cohabiter dans le même pod ou deux
Deployments distincts selon la charge).

Les tools appellent directement les fonctions Python du service (pas
d'aller-retour HTTP loopback). L'identité utilisateur (`user_sub`)
arrive en paramètre du tool — l'autorisation est de la responsabilité
du client MCP (Open WebUI, agent custom…).

Tools exposés :
  - video.import        : déclenche ingestion, renvoie {video_source_id, status, reused}
  - video.get_metadata  : métadonnées d'une source
  - video.get_transcript: transcript texte / segments / markdown
  - video.search        : recherche full-text PostgreSQL fr
  - video.purge         : purge admin

Lancement standalone :
  python -m services.video_ingest.app.mcp_server
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db
from . import jobs as jobs_mod
from .providers.youtube import YouTubeProvider
from .repo import (
    add_bookmark,
    find_source_by_provider_id,
    has_transcript,
)

log = logging.getLogger(__name__)

import os as _os

# Bind explicite 0.0.0.0:8001 — sinon FastMCP par défaut écoute sur
# 127.0.0.1, et le pod K8s n'est pas joignable depuis le Service ClusterIP.
mcp = FastMCP(
    "video-ingest",
    host=_os.environ.get("VIDEO_INGEST_MCP_BIND_HOST", "0.0.0.0"),
    port=int(_os.environ.get("VIDEO_INGEST_MCP_BIND_PORT", "8001")),
)

_PROVIDERS = [YouTubeProvider()]


def _route(url: str) -> tuple[str, str]:
    for p in _PROVIDERS:
        if p.matches_url(url):
            vid, _ = p.parse_canonical_id(url)
            return p.name, vid
    raise ValueError(f"URL non reconnue : {url!r}")


@mcp.tool()
def video_import(
    url: str,
    user_sub: str,
    *,
    language: str | None = None,
    force_audio: bool = False,
    context: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Importe une vidéo (HIT cache synchrone, MISS = enqueue async).

    Renvoie `{status, reused, video_source_id, job_id}`.
    """
    provider_name, provider_video_id = _route(url)
    with db.connection() as conn:
        existing = find_source_by_provider_id(
            conn, provider=provider_name, provider_video_id=provider_video_id,
        )
        if existing and has_transcript(conn, video_source_id=existing, language=language):
            bk = add_bookmark(
                conn, user_sub=user_sub, video_source_id=existing,
                context=context, context_id=context_id,
            )
            return {"status": "ready", "reused": True,
                    "video_source_id": existing, "bookmark_id": bk, "job_id": None}
    with db.connection() as conn:
        job_id = jobs_mod.enqueue(
            conn, url=url, user_sub=user_sub,
            context=context, context_id=context_id,
            language_pref=language, force_audio=force_audio,
        )
    return {"status": "pending", "reused": False,
            "video_source_id": None, "job_id": job_id}


@mcp.tool()
def video_get_job(job_id: int, user_sub: str) -> dict[str, Any]:
    """Statut d'un job d'ingestion pour polling client.

    Renvoie `{id, status, video_source_id, reused, error_message,
    attempts, created_at, completed_at}` ou `{error}` si introuvable
    ou si le job ne lui appartient pas.

    `status` ∈ {pending, running, done, failed}.
    """
    with db.cursor() as cur:
        cur.execute(
            """SELECT id, status, video_source_id, reused, error_message,
                      attempts, created_at, completed_at, user_sub
                 FROM video_ingest_jobs WHERE id = %s""",
            (job_id,),
        )
        row = cur.fetchone()
    if not row or row[8] != user_sub:
        return {"error": "job introuvable"}
    return {
        "id": row[0], "status": row[1], "video_source_id": row[2],
        "reused": row[3], "error_message": row[4], "attempts": row[5],
        "created_at": row[6].isoformat() if row[6] else None,
        "completed_at": row[7].isoformat() if row[7] else None,
    }


@mcp.tool()
def video_get_metadata(video_source_id: int) -> dict[str, Any]:
    """Métadonnées d'une source vidéo."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT provider, provider_video_id, canonical_url, title, channel,
                      duration_sec, published_at, metadata_json
                 FROM video_sources WHERE id = %s""",
            (video_source_id,),
        )
        row = cur.fetchone()
    if not row:
        return {"error": "source introuvable"}
    return {
        "id": video_source_id, "provider": row[0], "provider_video_id": row[1],
        "canonical_url": row[2], "title": row[3], "channel": row[4],
        "duration_sec": row[5],
        "published_at": row[6].isoformat() if row[6] else None,
        "metadata": row[7] or {},
    }


@mcp.tool()
def video_get_transcript(
    video_source_id: int,
    *,
    language: str | None = None,
    format: str = "text",
) -> dict[str, Any]:
    """Transcript d'une source. `format` ∈ {text, segments, markdown}."""
    with db.cursor() as cur:
        if language:
            cur.execute(
                """SELECT language, method, content_text, segments_json
                     FROM video_transcripts
                    WHERE video_source_id = %s AND language = %s
                    ORDER BY (method = 'subtitle_manual') DESC, created_at DESC LIMIT 1""",
                (video_source_id, language),
            )
        else:
            cur.execute(
                """SELECT language, method, content_text, segments_json
                     FROM video_transcripts
                    WHERE video_source_id = %s
                    ORDER BY (method = 'subtitle_manual') DESC, created_at DESC LIMIT 1""",
                (video_source_id,),
            )
        row = cur.fetchone()
    if not row:
        return {"error": "transcript introuvable"}
    base = {"language": row[0], "method": row[1]}
    segments = row[3] or []
    if format == "segments":
        base["segments"] = segments
    elif format == "markdown":
        base["markdown"] = "\n\n".join(
            f"**[{int(s.get('start_seconds',0))}s]** {s.get('text','')}".strip()
            for s in segments
        ) if segments else row[2]
    else:
        base["text"] = row[2]
    return base


@mcp.tool()
def video_search(query: str, *, limit: int = 20) -> dict[str, Any]:
    """Recherche full-text PostgreSQL (tsvector français)."""
    limit = min(int(limit), 100)
    with db.cursor() as cur:
        cur.execute(
            """SELECT t.video_source_id, s.title, s.canonical_url, t.language,
                      ts_rank(t.content_tsv, plainto_tsquery('french', %s)),
                      ts_headline('french', t.content_text,
                                  plainto_tsquery('french', %s),
                                  'MaxFragments=2,MaxWords=30')
                 FROM video_transcripts t
                 JOIN video_sources s ON s.id = t.video_source_id
                WHERE t.content_tsv @@ plainto_tsquery('french', %s)
                ORDER BY 5 DESC LIMIT %s""",
            (query, query, query, limit),
        )
        rows = cur.fetchall()
    return {
        "query": query,
        "results": [
            {"video_source_id": r[0], "title": r[1], "canonical_url": r[2],
             "language": r[3], "rank": float(r[4]), "snippet": r[5]}
            for r in rows
        ],
    }


@mcp.tool()
def video_purge(video_source_id: int, *, admin_token: str) -> dict[str, Any]:
    """Purge complète d'une source (admin). `admin_token` doit matcher
    la variable d'env `VIDEO_INGEST_MCP_ADMIN_TOKEN` (le client MCP est
    responsable de la passer en paramètre)."""
    import os
    expected = os.environ.get("VIDEO_INGEST_MCP_ADMIN_TOKEN")
    if not expected or admin_token != expected:
        return {"error": "admin_token invalide"}
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM video_sources WHERE id = %s RETURNING id",
                (video_source_id,),
            )
            row = cur.fetchone()
    if not row:
        return {"error": "source introuvable"}
    return {"status": "purged", "video_source_id": video_source_id}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport="streamable-http")
