"""Blueprint ``meetings`` — endpoints browser-facing ``/api/meetings/*``.

Refacto PR3 :

- ``GET    /api/meetings``                       — liste
- ``POST   /api/meetings``                       — création
- ``GET    /api/meetings/<id>``                  — détail
- ``PUT    /api/meetings/<id>``                  — alias amend
- ``POST   /api/meetings/<id>/amend``            — édition contenu
- ``POST   /api/meetings/<id>/rename``           — renomme
- ``DELETE /api/meetings/<id>``                  — soft-delete
- ``POST   /api/meetings/<id>/restore``          — restaure
- ``DELETE /api/meetings/<id>/permanently``      — hard-delete
- ``POST   /api/meetings/<id>/link-preparation`` — attache une préparation
- ``POST   /api/meetings/<id>/link-audio``       — attache un audio
- ``POST   /api/meetings/<id>/reprocess``        — relance la chaîne glossaire LLM

L'isolation cross-zone reste portée par ``device-token-authority``.
"""

from __future__ import annotations

import logging
import os

import requests as req
from flask import Blueprint, jsonify, request

from ...shared import (
    get_current_user,
    require_auth,
    trigger_audio_reprocess,
)
from libs.shared.app.config import INTERNAL_API_TOKEN  # noqa: E402
from libs.shared.app.security import verify_bearer_token  # noqa: E402
from . import service as meeting_service

logger = logging.getLogger("mesreunions_web.meetings.routes")

bp = Blueprint("meetings", __name__, url_prefix="/api/meetings")


def _err(reason: dict | str, status: int):
    if isinstance(reason, str):
        return jsonify({"error": reason}), status
    return jsonify(reason), status


# ─── Liste / création ────────────────────────────────────────────────

@bp.route("", methods=["GET"], strict_slashes=False)
@require_auth
def list_meetings():
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        data = meeting_service.list_meetings(user_sub)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "list_failed"}, status)
    return jsonify({"meetings": data.get("meetings", [])})


@bp.route("", methods=["POST"], strict_slashes=False)
@require_auth
def create_meeting():
    """Crée un meeting (peut exister standalone — sans audio ni préparation).

    Body : ``{title?, preparation_id?, file_id?, content?, ...}``.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = dict(request.get_json(silent=True) or {})
    payload["user_sub"] = user_sub
    try:
        data = meeting_service.create_meeting(payload)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        try:
            body = err.response.json() if err.response is not None else {}
        except Exception:
            body = {}
        return _err({"error": body.get("error", "create_failed")}, status)
    return jsonify(data)


# ─── Détail / mutations ──────────────────────────────────────────────

@bp.route("/<meeting_id>", methods=["GET"])
@require_auth
def get_meeting(meeting_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        data = meeting_service.get_meeting(user_sub, meeting_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        try:
            body = err.response.json() if err.response is not None else {}
        except Exception:
            body = {}
        return _err({"error": body.get("error", "get_failed")}, status)
    return jsonify({"meeting": data.get("meeting") or {}})


@bp.route("/<meeting_id>", methods=["PUT"])
@require_auth
def update_meeting(meeting_id: str):
    return _amend_impl(meeting_id)


@bp.route("/<meeting_id>/amend", methods=["POST"])
@require_auth
def amend_meeting(meeting_id: str):
    return _amend_impl(meeting_id)


def _amend_impl(meeting_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    new_content = payload.get("content")
    if not isinstance(new_content, dict):
        return _err("content must be an object", 400)
    try:
        data = meeting_service.amend_meeting(user_sub, meeting_id, new_content)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "amend_failed"}, status)
    return jsonify(data)


@bp.route("/<meeting_id>/rename", methods=["POST"])
@require_auth
def rename_meeting(meeting_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    new_title = (payload.get("title") or "").strip()
    if not new_title:
        return _err("title is required", 400)
    if len(new_title) > 120:
        return _err("title too long", 400)
    try:
        data = meeting_service.rename_meeting(user_sub, meeting_id, new_title)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "rename_failed"}, status)
    return jsonify({"ok": True, "title": data.get("title", new_title)})


@bp.route("/<meeting_id>", methods=["DELETE"])
@require_auth
def trash_meeting(meeting_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        meeting_service.trash_meeting(user_sub, meeting_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "delete_failed"}, status)
    return jsonify({"ok": True, "trashed": True})


@bp.route("/<meeting_id>/restore", methods=["POST"])
@require_auth
def restore_meeting(meeting_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        meeting_service.restore_meeting(user_sub, meeting_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "restore_failed"}, status)
    return jsonify({"ok": True, "restored": True})


@bp.route("/<meeting_id>/permanently", methods=["DELETE"])
@require_auth
def hard_delete_meeting(meeting_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        meeting_service.hard_delete_meeting(user_sub, meeting_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "delete_failed"}, status)
    return jsonify({"ok": True, "deleted": True})


# ─── Liaison ────────────────────────────────────────────────────────

@bp.route("/<meeting_id>/link-preparation", methods=["POST"])
@require_auth
def link_meeting_to_preparation(meeting_id: str):
    """Lie ou délie une préparation à ce meeting. Body : ``{preparation_id|null}``."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    prep_id = payload.get("preparation_id")
    if isinstance(prep_id, str):
        prep_id = prep_id.strip() or None
    try:
        data = meeting_service.link_preparation(user_sub, meeting_id, prep_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "link_failed"}, status)
    return jsonify(data)


@bp.route("/<meeting_id>/link-audio", methods=["POST"])
@require_auth
def link_meeting_to_audio(meeting_id: str):
    """Lie/délie un audio à ce meeting. Body : ``{file_id|null}``."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    file_id = payload.get("file_id")
    if isinstance(file_id, str):
        file_id = file_id.strip() or None
    try:
        data = meeting_service.link_audio(user_sub, meeting_id, file_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "link_failed"}, status)
    return jsonify(data)


# ─── Reprocess (regénération CR depuis nouveau glossaire) ──────────

@bp.route("/<meeting_id>/reprocess", methods=["POST"])
@require_auth
def reprocess_meeting(meeting_id: str):
    """Regénère le CR du meeting avec le glossaire à jour.

    Récupère l'audio lié au meeting (via le détail meeting → file_id),
    puis appelle ``internal-ingester /api/v1/audio/<file_id>/reprocess`` qui
    relance la chaîne glossary_correction → reformulation → meeting_analysis.

    Body optionnel : ``{glossary_from_preparation_id?: <uuid>, force?: bool}``.
    Si non fourni, internal-ingester re-utilise la préparation déjà liée.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    prep_id = payload.get("glossary_from_preparation_id")
    force = bool(payload.get("force"))

    # Récupère le file_id via le meeting.
    try:
        meeting_data = meeting_service.get_meeting(user_sub, meeting_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "meeting_not_found"}, status)
    meeting = meeting_data.get("meeting") or {}
    file_id = meeting.get("user_audio_file_id") or meeting.get("file_id")
    if not file_id:
        return _err("Ce meeting n'est pas lié à un audio — reprocess impossible.", 400)

    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL") or ""
    if not base:
        return _err({"error": "reprocess_not_configured"}, 503)
    try:
        resp = req.post(
            f"{base.rstrip('/')}/api/v1/audio/{file_id}/reprocess",
            json={
                "user_sub": user_sub,
                "glossary_from_preparation_id": prep_id,
                "glossary_from_brief_id": prep_id,  # alias legacy
                "force": force,
            },
            headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
            timeout=10,
        )
    except Exception as exc:
        logger.exception("meetings: internal-ingester unreachable: %s", exc)
        return _err({"error": "internal_ingester_unreachable"}, 502)
    if resp.status_code >= 400:
        try:
            return jsonify(resp.json()), resp.status_code
        except Exception:
            return _err({"error": "reprocess_failed"}, resp.status_code)
    try:
        return jsonify(resp.json())
    except Exception:
        return jsonify({"ok": True})


# ─── Email CR (Lot 8 PR-6) ──────────────────────────────────────────

def _verify_internal_bearer() -> bool:
    """Vérifie un Authorization: Bearer <INTERNAL_API_TOKEN>.

    Utilisé par les endpoints "system-to-system" (hook puller post-
    transcription) qui n'ont pas de session OIDC.
    """
    # Comparaison à temps constant via le helper partagé (évite l'oracle de
    # timing d'un `==` sur secret).
    return verify_bearer_token(request.headers.get("Authorization", ""), INTERNAL_API_TOKEN)


@bp.route("/<meeting_id>/send-cr", methods=["POST"])
def send_meeting_cr(meeting_id: str):
    """Envoie le CR du meeting par email aux participants[].email de la prep.

    Accepte 2 modes d'auth :
    - OIDC (session navigateur) : le bouton "Envoyer le CR maintenant" UX.
    - Bearer ``INTERNAL_API_TOKEN`` : appelé par le hook post-transcription
      côté dmz-to-internal-bridge (puller) après remplissage de meeting.content.

    Body optionnel :
      - ``user_sub`` : requis si appelé en mode internal (sinon dérivé OIDC)
      - ``recipients`` : liste d'emails (sinon dérivée de prep.participants)
      - ``force`` : ignore le toggle ``send_cr_email`` (défaut False)
    """
    from ...shared import (
        request_internal_preparation_api,
        request_internal_meeting_api,
    )
    from app.mailer import is_configured as _smtp_configured
    from app.mailer import send_meeting_cr_email as _send_cr

    payload = request.get_json(silent=True) or {}
    internal_auth = _verify_internal_bearer()

    if internal_auth:
        user_sub = (payload.get("user_sub") or "").strip()
        if not user_sub:
            return _err("user_sub required for internal call", 400)
    else:
        user = get_current_user()
        if not user:
            return _err("Unauthorized", 401)
        user_sub = (user or {}).get("sub") or ""

    if not _smtp_configured():
        return _err({"error": "smtp_not_configured"}, 503)

    # Charge le meeting + la prep (si liée).
    try:
        m_resp = request_internal_meeting_api(
            "GET", f"/api/v1/meetings/{meeting_id}",
            params={"user_sub": user_sub},
        )
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "meeting_not_found"}, status)
    meeting = (m_resp or {}).get("meeting") or {}
    prep_id = meeting.get("preparation_id")
    preparation = None
    if prep_id:
        try:
            p_resp = request_internal_preparation_api(
                "GET", f"/api/v1/preparations/{prep_id}",
                params={"user_sub": user_sub},
            )
            preparation = (p_resp or {}).get("preparation") or {}
        except Exception:
            logger.exception("send-cr: failed to load preparation %s", prep_id)
            preparation = None

    # Respecte le toggle send_cr_email sauf override explicite (?force=true).
    force = bool(payload.get("force"))
    if not force and preparation is not None:
        if not preparation.get("send_cr_email"):
            return jsonify({
                "ok": False, "sent": 0,
                "skipped_reason": "toggle_disabled",
            })

    recipients = payload.get("recipients")
    if recipients is not None and not isinstance(recipients, list):
        return _err("recipients must be a list of strings", 400)

    public_base = os.getenv("PUBLIC_BASE_URL") or ""
    result = _send_cr(
        meeting=meeting,
        preparation=preparation,
        recipients=recipients,
        public_base_url=public_base,
    )
    status = 200 if result.get("ok") else 200  # best-effort, on ne casse jamais
    return jsonify(result), status

