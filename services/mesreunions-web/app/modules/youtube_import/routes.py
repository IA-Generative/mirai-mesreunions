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
    """C5 — placeholder Meeting immédiat (variation insert+update).

    Workflow :
      1. Crée IMMÉDIATEMENT un Meeting placeholder côté device-token-authority
         (title = URL brute, video_source_id et video_ingest_job_id null
         pour l'instant — seront posés par la suite via PATCH ou re-POST
         idempotent côté materialize).
      2. Forward la demande à video-ingest avec context_id=meeting_id ;
         le hook materialize (C4) côté video-ingest passera ce meeting_id
         à internal-ingester qui liera l'UAF virtuel au Meeting placeholder.
      3. Renvoie {meeting_id, job_id?, video_source_id?} au front pour
         qu'il puisse rafraîchir la liste tout de suite (avec status
         intermédiaire "en cours d'import").
    """
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url requise"}), 400

    user_sub = _user_sub()
    if not user_sub:
        return jsonify({"error": "user inconnu"}), 401

    # 1. Création immédiate du placeholder Meeting (best-effort).
    placeholder_meeting_id = _create_placeholder_meeting(user_sub=user_sub, url=url)

    # 2. Forward à video-ingest avec context_id = meeting_id (le hook
    #    materialize côté video-ingest passera ce meeting_id à
    #    internal-ingester).
    forwarded = {
        "url": url,
        "language": payload.get("language", "fr"),
        "force_audio": bool(payload.get("force_audio", False)),
        "context": "meeting",
        "context_id": placeholder_meeting_id or payload.get("context_id"),
    }
    result, err = _call_video_ingest("POST", "/video/import",
                                      json_body=forwarded, timeout=15)
    if err:
        msg, status = err
        return jsonify({
            "error": msg,
            "meeting_id": placeholder_meeting_id,  # exposé pour rollback front
        }), status
    status, body = result

    # 3. HIT cache (200 + reused=true) → lier la source directement au meeting.
    if status == 200 and body.get("reused") and body.get("video_source_id"):
        try:
            m = _ensure_meeting_from_video(
                user_sub=user_sub, video_source_id=body["video_source_id"],
                video_ingest_job_id=None,
                existing_meeting_id=placeholder_meeting_id,
            )
            if m:
                body["meeting_id"] = m.get("id")
        except Exception:
            logger.exception("création Meeting (HIT cache) a échoué — non bloquant")
    elif placeholder_meeting_id:
        # MISS — on remonte le meeting_id pour que le front rafraîchisse
        # la liste immédiatement avec le placeholder.
        body["meeting_id"] = placeholder_meeting_id

    return jsonify(body), status


def _create_placeholder_meeting(*, user_sub: str, url: str) -> str | None:
    """Crée un Meeting placeholder via device-token-authority.

    Title temporaire = URL pour que la row apparaisse dans la liste
    avec un libellé identifiable. Sera remplacé par suggested_filename
    LLM quand le pipeline meeting-intelligence aura fini.

    Best-effort : si l'appel échoue, on continue sans (le placeholder
    apparaîtra plus tard via le polling).
    """
    try:
        resp = request_internal_device_api(
            "POST", "/api/v1/meetings",
            json_body={
                "user_sub": user_sub,
                "title": (url[:200] if url else "Import en cours"),
            },
            timeout=10,
        )
        meeting = (resp or {}).get("meeting") or {}
        return meeting.get("id")
    except Exception:
        logger.exception("placeholder Meeting creation failed (non-fatal)")
        return None


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


# ─── DELETE /meetings/<id> : suppression d'un import YouTube ──────────────

@bp.delete("/meetings/<meeting_id>")
@require_auth
def delete_youtube_meeting(meeting_id: str):
    """Mise à la corbeille d'un Meeting issu d'un import YouTube.

    Délègue à device-token-authority DELETE /api/v1/meetings/<id> qui
    soft-delete le meeting (pose trashed_at). L'UAF lié (s'il existe)
    reste avec sa transcription et son CR — la fiche détail reste
    consultable depuis la corbeille via les endpoints existants.
    """
    user_sub = _user_sub()
    if not user_sub:
        return jsonify({"error": "user inconnu"}), 401

    try:
        resp = request_internal_device_api(
            "DELETE", f"/api/v1/meetings/{meeting_id}",
            json_body={"user_sub": user_sub},
            timeout=10,
        )
        return jsonify(resp or {"ok": True}), 200
    except req.HTTPError:
        logger.exception("DELETE meeting failed for %s", meeting_id)
        return jsonify({"error": "suppression indisponible"}), 502


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

    # 1. Meetings YouTube côté device-token-authority avec preview UAF
    # (C6 — transcription_status, suggested_filename, key_points_summary).
    try:
        meetings_resp = request_internal_device_api(
            "GET", "/api/v1/meetings",
            params={
                "user_sub": user_sub,
                "only_video": "1",
                "with_audio_preview": "1",
                "limit": "200",
            },
        )
    except req.HTTPError:
        logger.exception("list meetings only_video failed")
        return jsonify({"error": "liste meetings indisponible"}), 502
    meetings = (meetings_resp or {}).get("meetings", []) or []

    # 2. Bookmarks video-ingest pour enrichir (titre vidéo brut, durée,
    # transcript stats côté video_ingest.video_transcripts).
    bookmarks_by_source = {}
    result, err = _call_video_ingest("GET", "/video/my-bookmarks")
    if not err:
        _status, body = result
        for b in body.get("bookmarks", []):
            bookmarks_by_source[b.get("video_source_id")] = b
    else:
        logger.warning("my-bookmarks enrichment failed (non-fatal): %s", err)

    # 3. Merge. Le titre courant prend en priorité le suggested_filename
    # LLM (C6 — vient du UAF post-pipeline) puis le titre vidéo brut puis
    # le title placeholder.
    out = []
    for m in meetings:
        vsid = m.get("video_source_id")
        meta = bookmarks_by_source.get(vsid, {})
        audio_preview = m.get("audio_preview") or {}
        # Calcul du materialization_status pour le front :
        # - 'pending' : UAF pas encore créé (placeholder seul)
        # - 'processing' : UAF en cours de pipeline (kevent_processing/transcribing)
        # - 'done' : UAF terminé (kevent_completed/partially)
        # - 'failed' : UAF en échec (kevent_failed)
        ts = audio_preview.get("transcription_status") or ""
        if not audio_preview:
            materialization_status = "pending"
        elif ts.startswith("kevent_completed") or ts == "kevent_partially_completed":
            materialization_status = "done"
        elif ts == "kevent_failed" or ts.startswith("mcr_") and "failed" in ts:
            materialization_status = "failed"
        else:
            materialization_status = "processing"

        out.append({
            "meeting_id": m.get("id"),
            "user_audio_file_id": m.get("user_audio_file_id"),
            "created_at": m.get("created_at"),
            "video_source_id": vsid,
            "video_ingest_job_id": m.get("video_ingest_job_id"),
            "title": (audio_preview.get("suggested_filename")
                      or meta.get("title")
                      or m.get("title")
                      or "(sans titre)"),
            "channel": meta.get("channel"),
            "duration_sec": meta.get("duration_sec"),
            "canonical_url": meta.get("canonical_url"),
            "transcript_language": meta.get("transcript_language"),
            "transcript_chars": meta.get("transcript_chars"),
            "transcript_method": meta.get("transcript_method"),
            "has_transcript": meta.get("has_transcript", False),
            # C6 — preview CR pour la fiche détail + status liste dynamique
            "materialization_status": materialization_status,
            "transcription_status": audio_preview.get("transcription_status"),
            "key_points_summary": audio_preview.get("key_points_summary"),
            "has_meeting_analysis": audio_preview.get("has_meeting_analysis", False),
        })
    return jsonify({"items": out})


# ─── Helper : création Meeting idempotente ─────────────────────────────

def _ensure_meeting_from_video(
    *, user_sub: str | None, video_source_id: int,
    video_ingest_job_id: int | None,
    existing_meeting_id: str | None = None,
) -> dict | None:
    """Appelle device-token-authority POST /api/v1/meetings.

    Idempotence côté serveur : si un Meeting existe déjà pour ce
    `video_ingest_job_id`, l'API renvoie `reused=true` avec le Meeting
    existant.

    Si `existing_meeting_id` est fourni (cas C5 — placeholder déjà créé
    avant l'appel video-ingest), on tente d'abord de lier la source vidéo
    à ce placeholder via PATCH ; si ça échoue, fallback sur POST classique.
    """
    if not user_sub or not video_source_id:
        return None

    # C5 — placeholder pré-créé : on lie la source au Meeting existant.
    if existing_meeting_id:
        try:
            patch_resp = request_internal_device_api(
                "PATCH", f"/api/v1/meetings/{existing_meeting_id}/link-video",
                json_body={
                    "user_sub": user_sub,
                    "video_source_id": int(video_source_id),
                    "video_ingest_job_id": int(video_ingest_job_id) if video_ingest_job_id is not None else None,
                },
                timeout=10,
            )
            meeting = (patch_resp or {}).get("meeting")
            if meeting:
                logger.info("Meeting placeholder %s linked to video_source=%s",
                            existing_meeting_id, video_source_id)
                return meeting
        except Exception:
            # Endpoint pas dispo (ancienne version device-token-authority) ou
            # autre échec → fallback sur POST classique ci-dessous.
            logger.warning("PATCH /meetings/<id>/link-video failed, fallback to POST")

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
