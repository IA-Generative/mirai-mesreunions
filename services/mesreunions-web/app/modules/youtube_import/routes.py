"""Proxy HTTP vers le service `video-ingest`.

Pattern aligné sur `modules/feedback/routes.py` (autre proxy serveur→
serveur du même repo). L'access_token OIDC de l'utilisateur est forwardé
en Bearer — video-ingest re-vérifie le JWT via JWKS Keycloak (mêmes
clés que mesreunions-web).
"""
from __future__ import annotations

import logging
import os

import requests as req
from flask import Blueprint, jsonify, request, session

from app.shared import get_current_user, require_auth

bp = Blueprint("youtube_import", __name__, url_prefix="/api/youtube")
logger = logging.getLogger("mesreunions_web.youtube_import")


def _video_ingest_base() -> str:
    """URL du service video-ingest (port 8000 par défaut)."""
    return os.getenv(
        "VIDEO_INGEST_BASE_URL",
        "http://video-ingest.audio-internal.svc.cluster.local:8000",
    ).rstrip("/")


def _bearer() -> str | None:
    """Récupère l'access_token OIDC stocké en session au login."""
    return session.get("access_token") or None


@bp.post("/import")
@require_auth
def import_youtube():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url requise"}), 400

    bearer = _bearer()
    if not bearer:
        # Session sans access_token (login antérieur à la slice 6 ?) → reconnexion.
        return jsonify({"error": "session expirée, reconnexion requise"}), 401

    forwarded = {
        "url": url,
        "language": payload.get("language", "fr"),
        "force_audio": bool(payload.get("force_audio", False)),
        "context": "meeting",  # video-ingest sait que c'est pour Mes Réunions
        "context_id": payload.get("context_id"),
    }
    try:
        resp = req.post(
            f"{_video_ingest_base()}/video/import",
            headers={"Authorization": f"Bearer {bearer}"},
            json=forwarded,
            timeout=15,
        )
    except req.RequestException as exc:
        logger.exception("video-ingest unreachable")
        return jsonify({"error": f"video-ingest injoignable: {exc}"}), 502

    try:
        body = resp.json()
    except ValueError:
        body = {"error": "réponse video-ingest non-JSON", "raw": resp.text[:500]}
    return jsonify(body), resp.status_code


@bp.get("/jobs/<int:job_id>")
@require_auth
def get_job(job_id: int):
    bearer = _bearer()
    if not bearer:
        return jsonify({"error": "session expirée"}), 401
    try:
        resp = req.get(
            f"{_video_ingest_base()}/video/jobs/{job_id}",
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=10,
        )
    except req.RequestException as exc:
        return jsonify({"error": f"video-ingest injoignable: {exc}"}), 502
    try:
        return jsonify(resp.json()), resp.status_code
    except ValueError:
        return jsonify({"error": "réponse video-ingest non-JSON"}), 502
