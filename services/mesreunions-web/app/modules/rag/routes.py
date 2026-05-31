"""Blueprint ``rag`` — endpoints ``/api/rag/*`` (widget « Interroger mes réunions »).

Proxy authentifié vers OpenRAG. Scoping FORCÉ server-side : chaque utilisateur
n'interroge/indexe QUE sa partition ``perso-{user_sub}`` (jamais une partition
fournie par le client). Auth widget = session OIDC mesreunions (require_auth) ;
auth OpenRAG = token admin de service (OPENRAG_API_KEY).
"""
from __future__ import annotations

import logging
import os

import requests as req
from flask import Blueprint, jsonify, request

from app.shared import get_current_user, require_auth  # insère aussi le repo root dans sys.path
from app.modules.rag import _openrag
from libs.shared.app.config import INTERNAL_API_TOKEN

bp = Blueprint("rag", __name__)
logger = logging.getLogger("mesreunions_web.rag")

# Cap d'ingestion par appel (sécurité / volumétrie).
_INGEST_CAP = int(os.getenv("RAG_INGEST_CAP", "60"))


def _partition_for(user_sub: str) -> str:
    """Partition perso de l'utilisateur. Scoping non-négociable côté serveur."""
    return "perso-" + (user_sub or "").strip()


def _ingester_base() -> str:
    return (os.getenv("FILE_PULLER_INTERNAL_BASE_URL") or "").rstrip("/")


# ── Query ─────────────────────────────────────────────────────────────
@bp.route("/api/rag/query", methods=["POST"])
@require_auth
def rag_query():
    user = get_current_user() or {}
    user_sub = (user.get("sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    if not _openrag.configured():
        return jsonify({"error": "rag_not_configured"}), 503
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question_required"}), 400
    history = body.get("history") if isinstance(body.get("history"), list) else None
    partition = _partition_for(user_sub)
    try:
        _openrag.ensure_partition(partition)
        res = _openrag.chat(partition, question, history=history)
        return jsonify(res), 200
    except req.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        logger.warning("rag_query HTTP %s: %s", code, (e.response.text[:200] if e.response is not None else e))
        return jsonify({"error": "rag_upstream", "code": code}), 502
    except Exception as e:
        logger.exception("rag_query failed")
        return jsonify({"error": "rag_query_failed", "detail": str(e)[:200]}), 502


# ── Status (combien de réunions indexées) ─────────────────────────────
@bp.route("/api/rag/status", methods=["GET"])
@require_auth
def rag_status():
    user = get_current_user() or {}
    user_sub = (user.get("sub") or "").strip()
    if not _openrag.configured():
        return jsonify({"configured": False, "indexed_count": 0}), 200
    try:
        files = _openrag.partition_files(_partition_for(user_sub))
        return jsonify({"configured": True, "indexed_count": len(files)}), 200
    except Exception:
        logger.debug("rag_status partition_files failed", exc_info=True)
        return jsonify({"configured": True, "indexed_count": 0}), 200


# ── Ingestion « Indexer mes réunions » ────────────────────────────────
def _fetch_rag_export(user_sub: str) -> list:
    base = _ingester_base()
    if not base:
        logger.info("rag ingest: FILE_PULLER_INTERNAL_BASE_URL absent → skip")
        return []
    r = req.get(
        f"{base}/api/v1/audio/rag-export",
        params={"user_sub": user_sub},
        headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json() if r.text else {}
    return body.get("items", []) if isinstance(body, dict) else []


def _compose_doc(it: dict) -> str:
    parts = [f"# {it.get('title') or 'Réunion'}"]
    if it.get("meeting_datetime"):
        parts.append(f"_Date de réunion : {it['meeting_datetime']}_")
    elif it.get("created_at"):
        parts.append(f"_Importée le : {it['created_at']}_")
    if it.get("key_points_summary"):
        parts.append("## Points clés\n\n" + str(it["key_points_summary"]))
    if it.get("meeting_analysis"):
        parts.append("## Analyse de la réunion\n\n" + str(it["meeting_analysis"]))
    if it.get("text"):
        parts.append("## Transcription\n\n" + str(it["text"]))
    return "\n\n".join(p for p in parts if p and p.strip()).strip()


@bp.route("/api/rag/ingest", methods=["POST"])
@require_auth
def rag_ingest():
    user = get_current_user() or {}
    user_sub = (user.get("sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    if not _openrag.configured():
        return jsonify({"error": "rag_not_configured"}), 503
    partition = _partition_for(user_sub)
    try:
        items = _fetch_rag_export(user_sub)
    except Exception as e:
        logger.exception("rag ingest: rag-export failed")
        return jsonify({"error": "export_failed", "detail": str(e)[:200]}), 502
    try:
        _openrag.ensure_partition(partition)
    except Exception as e:
        logger.exception("rag ingest: ensure_partition failed")
        return jsonify({"error": "partition_failed", "detail": str(e)[:200]}), 502

    queued = skipped = failed = 0
    capped = items[:_INGEST_CAP]
    for it in capped:
        uaf_id = (it.get("uaf_id") or "").strip()
        doc = _compose_doc(it)
        if not uaf_id or not doc:
            skipped += 1
            continue
        try:
            r = _openrag.index_text(
                partition, uaf_id, it.get("title") or "Réunion", doc,
                metadata={
                    "source": it.get("source_type") or it.get("origin") or "meeting",
                    "meeting_id": it.get("meeting_id"),
                    "meeting_datetime": it.get("meeting_datetime"),
                    "created_at": it.get("created_at"),
                },
            )
            st = r.get("status")
            if st == "queued":
                queued += 1
            elif st == "exists":
                skipped += 1
            else:
                failed += 1
        except Exception:
            logger.exception("rag ingest: index_text failed for %s", uaf_id)
            failed += 1
    logger.info("rag ingest user=%s total=%d queued=%d skipped=%d failed=%d",
                user_sub[:12], len(items), queued, skipped, failed)
    return jsonify({
        "total": len(items), "considered": len(capped),
        "queued": queued, "skipped": skipped, "failed": failed,
        "capped": len(items) > _INGEST_CAP,
    }), 200
