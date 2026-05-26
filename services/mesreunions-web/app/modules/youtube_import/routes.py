"""Proxy HTTP vers le service `video-ingest` + wiring Meeting (slice 6 C2).

Architecture :
- POST /api/youtube/import  → proxy /video/import (auth: Bearer OIDC user)
- GET  /api/youtube/jobs/<id> → proxy /video/jobs/<id> ; **si status=done**,
  on crée AUSSI un Meeting (idempotent via video_ingest_job_id) en
  appelant device-token-authority POST /api/v1/meetings.
- GET  /api/youtube/my-imports → liste les Meetings YouTube de l'user
  (Meeting.video_source_id IS NOT NULL) enrichis par GET /video/my-bookmarks
  de video-ingest pour titre + durée + stats transcript.

Pattern aligné sur `modules/feedback/routes.py`. L'access_token OIDC
de l'utilisateur est forwardé en Bearer à video-ingest, les écritures
Meeting passent par INTERNAL_API_TOKEN (zone interne).
"""
from __future__ import annotations

import logging
import os

import requests as req
from flask import Blueprint, jsonify, request, session

from app.shared import get_current_user, request_internal_device_api, require_auth

bp = Blueprint("youtube_import", __name__, url_prefix="/api/youtube")
logger = logging.getLogger("mesreunions_web.youtube_import")


def _video_ingest_base() -> str:
    return os.getenv(
        "VIDEO_INGEST_BASE_URL",
        "http://video-ingest.audio-internal.svc.cluster.local:8000",
    ).rstrip("/")


def _bearer() -> str | None:
    return session.get("access_token") or None


def _user_sub() -> str | None:
    u = get_current_user() or {}
    return u.get("sub") or None


def _call_video_ingest(method: str, path: str, *, json_body=None, timeout: int = 10):
    """Appel proxyé vers video-ingest avec le Bearer OIDC de l'user."""
    bearer = _bearer()
    if not bearer:
        return None, ("session expirée, reconnexion requise", 401)
    try:
        resp = req.request(
            method, f"{_video_ingest_base()}{path}",
            headers={"Authorization": f"Bearer {bearer}"},
            json=json_body, timeout=timeout,
        )
    except req.RequestException:
        # On NE remonte PAS `{exc}` côté HTTP : peut contenir URL/headers
        # internes ou stacktrace (CodeQL py/stack-trace-exposure).
        logger.exception("video-ingest unreachable")
        return None, ("video-ingest injoignable", 502)
    try:
        return (resp.status_code, resp.json()), None
    except ValueError:
        return None, ("réponse video-ingest non-JSON", 502)


# ─── POST /import ──────────────────────────────────────────────────────

@bp.post("/import")
@require_auth
def import_youtube():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url requise"}), 400

    forwarded = {
        "url": url,
        "language": payload.get("language", "fr"),
        "force_audio": bool(payload.get("force_audio", False)),
        "context": "meeting",
        "context_id": payload.get("context_id"),
    }
    result, err = _call_video_ingest("POST", "/video/import",
                                      json_body=forwarded, timeout=15)
    if err:
        msg, status = err
        return jsonify({"error": msg}), status
    status, body = result

    # HIT cache (200 + reused=true) → on crée TOUT DE SUITE le Meeting,
    # pas besoin d'attendre un polling.
    if status == 200 and body.get("reused") and body.get("video_source_id"):
        try:
            _ensure_meeting_from_video(
                user_sub=_user_sub(), video_source_id=body["video_source_id"],
                video_ingest_job_id=None,  # HIT cache : pas de job
            )
        except Exception:
            logger.exception("création Meeting (HIT cache) a échoué — non bloquant")
    return jsonify(body), status


# ─── GET /jobs/<id> + création Meeting au done ─────────────────────────

@bp.get("/jobs/<int:job_id>")
@require_auth
def get_job(job_id: int):
    result, err = _call_video_ingest("GET", f"/video/jobs/{job_id}")
    if err:
        msg, status = err
        return jsonify({"error": msg}), status
    status, body = result

    # Status terminal `done` → on crée le Meeting si pas déjà fait.
    # Idempotent : device-token-authority renvoie reused=true si déjà existant.
    if (status == 200 and body.get("status") == "done"
            and body.get("video_source_id")):
        try:
            m = _ensure_meeting_from_video(
                user_sub=_user_sub(),
                video_source_id=body["video_source_id"],
                video_ingest_job_id=job_id,
            )
            if m:
                body["meeting_id"] = m.get("id")
        except Exception:
            logger.exception("création Meeting (done) a échoué — non bloquant")
    return jsonify(body), status


# ─── GET /my-imports : liste enrichie ──────────────────────────────────

@bp.get("/my-imports")
@require_auth
def my_imports():
    """Liste des meetings YouTube de l'user, enrichis par les metadata
    (titre, channel, durée, stats transcript) issues de video-ingest.
    """
    user_sub = _user_sub()
    if not user_sub:
        return jsonify({"error": "user inconnu"}), 401

    # 1. Meetings YouTube côté device-token-authority (filter only_video=1).
    try:
        meetings_resp = request_internal_device_api(
            "GET", "/api/v1/meetings",
            params={"user_sub": user_sub, "only_video": "1", "limit": "200"},
        )
    except req.HTTPError:
        # Idem : on logue mais on n'expose pas le détail HTTP.
        logger.exception("list meetings only_video failed")
        return jsonify({"error": "liste meetings indisponible"}), 502
    meetings = (meetings_resp or {}).get("meetings", []) or []

    # 2. Bookmarks video-ingest pour enrichir (titre, durée, transcript stats).
    bookmarks_by_source = {}
    result, err = _call_video_ingest("GET", "/video/my-bookmarks")
    if not err:
        _status, body = result
        for b in body.get("bookmarks", []):
            bookmarks_by_source[b.get("video_source_id")] = b
    else:
        logger.warning("my-bookmarks enrichment failed (non-fatal): %s", err)

    # 3. Merge.
    out = []
    for m in meetings:
        vsid = m.get("video_source_id")
        meta = bookmarks_by_source.get(vsid, {})
        out.append({
            "meeting_id": m.get("id"),
            "created_at": m.get("created_at"),
            "video_source_id": vsid,
            "video_ingest_job_id": m.get("video_ingest_job_id"),
            "title": meta.get("title") or m.get("title") or "(sans titre)",
            "channel": meta.get("channel"),
            "duration_sec": meta.get("duration_sec"),
            "canonical_url": meta.get("canonical_url"),
            "transcript_language": meta.get("transcript_language"),
            "transcript_chars": meta.get("transcript_chars"),
            "transcript_method": meta.get("transcript_method"),
            "has_transcript": meta.get("has_transcript", False),
        })
    return jsonify({"items": out})


# ─── Helper : création Meeting idempotente ─────────────────────────────

def _ensure_meeting_from_video(
    *, user_sub: str | None, video_source_id: int,
    video_ingest_job_id: int | None,
) -> dict | None:
    """Appelle device-token-authority POST /api/v1/meetings.

    Idempotence côté serveur : si un Meeting existe déjà pour ce
    `video_ingest_job_id`, l'API renvoie `reused=true` avec le Meeting
    existant. Pour les HIT cache (job_id=None), on ne peut pas être
    idempotent par job_id → on accepte le risque d'un doublon (rare,
    l'utilisateur clique 2 fois sur Importer en moins de 2 s sur la
    même URL déjà ingérée).
    """
    if not user_sub or not video_source_id:
        return None
    body: dict = {
        "user_sub": user_sub,
        "video_source_id": int(video_source_id),
    }
    if video_ingest_job_id is not None:
        body["video_ingest_job_id"] = int(video_ingest_job_id)
    resp = request_internal_device_api("POST", "/api/v1/meetings",
                                        json_body=body, timeout=10)
    meeting = (resp or {}).get("meeting")
    if meeting and (resp or {}).get("reused"):
        logger.info("Meeting réutilisé pour video_source=%s job=%s id=%s",
                    video_source_id, video_ingest_job_id, meeting.get("id"))
    elif meeting:
        logger.info("Meeting créé pour video_source=%s job=%s id=%s",
                    video_source_id, video_ingest_job_id, meeting.get("id"))
    return meeting
