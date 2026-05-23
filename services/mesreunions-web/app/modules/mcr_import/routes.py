"""Blueprint ``mcr_import`` — endpoints ``/api/mcr/*``.

Permet à un utilisateur authentifié sur mesreunions-web d'importer une (ou
plusieurs) réunion(s) depuis la plateforme MCR (``compte-rendu.mirai``).

Endpoints :

- ``GET  /api/mcr/meetings?page=&page_size=&search=`` — relai paginé vers MCR
- ``POST /api/mcr/import``                           — déclenche l'import async

Le pull effectif (audio webm → S3 + entrée pipeline / DOCX → transcription_text)
est délégué au worker ``mcr_importer`` côté internal-ingester via la queue
RabbitMQ ``QUEUE_MCR_IMPORT``.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timezone

import requests as req
from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))

from libs.shared.app.mirai_oidc import (  # noqa: E402
    OIDCApplicativeError,
    OIDCAuthError,
    OIDCTransientError,
    exchange_refresh_token,
)
from libs.shared.app.oidc_refresh_store import (  # noqa: E402
    delete_ciphertext,
    fetch_ciphertext,
)
from libs.shared.app.queue_helper import QUEUE_MCR_IMPORT, publish_message  # noqa: E402
from libs.shared.app.secrets_crypto import decrypt  # noqa: E402

from ...shared import get_current_user, require_auth
from ...runtime import get_rabbit_cfg, session_scope

logger = logging.getLogger("mesreunions_web.mcr_import.routes")

bp = Blueprint("mcr_import", __name__, url_prefix="/api/mcr")


def _err(reason: str, status: int):
    return jsonify({"error": reason}), status


def _mcr_base() -> str:
    base = os.getenv("MCR_GATEWAY_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("MCR_GATEWAY_URL not configured")
    return base


def _get_user_access_token(user_sub: str) -> str:
    """Recover the user's access token by decrypting the stored refresh and
    exchanging it on Mirai. Raises OIDC* errors that the caller maps to HTTP.
    """
    ciphertext = fetch_ciphertext(user_sub)
    if not ciphertext:
        raise OIDCAuthError("no_refresh_token_stored")
    try:
        refresh_token = decrypt(ciphertext)
    except Exception as exc:
        raise OIDCApplicativeError(f"refresh_token_decrypt_failed: {exc}") from exc
    token_endpoint = os.getenv("OIDC_TOKEN_ENDPOINT", "")
    client_id = os.getenv("OIDC_CLIENT_ID", "")
    client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
    try:
        return exchange_refresh_token(
            token_endpoint=token_endpoint,
            client_id=client_id,
            refresh_token=refresh_token,
            client_secret=client_secret,
            timeout=10,
        )
    except OIDCAuthError:
        # Refresh expired/revoked: wipe so the next attempt fails fast and
        # the user is prompted to log in again.
        try:
            delete_ciphertext(user_sub)
        except Exception:
            logger.exception("delete_ciphertext failed for user_sub=%s", user_sub)
        raise


def _handle_oidc_exc(exc: Exception):
    if isinstance(exc, OIDCAuthError):
        return _err("auth_required_reconnect", 401)
    if isinstance(exc, OIDCTransientError):
        return _err("sso_unreachable", 502)
    return _err(f"oidc_error: {exc}", 500)


# ─── Liste des réunions sur MCR ──────────────────────────────────────

@bp.route("/meetings", methods=["GET"], strict_slashes=False)
@require_auth
def list_mcr_meetings():
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return _err("no_user_sub", 401)

    page = max(1, int(request.args.get("page", "1") or "1"))
    page_size = max(1, min(50, int(request.args.get("page_size", "20") or "20")))
    search = (request.args.get("search") or "").strip() or None

    try:
        access_token = _get_user_access_token(user_sub)
    except (OIDCAuthError, OIDCApplicativeError, OIDCTransientError) as exc:
        return _handle_oidc_exc(exc)
    except Exception:
        logger.exception("list_mcr_meetings: unexpected error getting access token")
        return _err("internal_error", 500)

    try:
        url = f"{_mcr_base()}/api/meetings/"
        params = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        resp = req.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except req.RequestException:
        logger.exception("MCR /meetings unreachable")
        return _err("mcr_unreachable", 502)
    if resp.status_code in (401, 403):
        return _err("mcr_forbidden", 403)
    if resp.status_code >= 500:
        return _err(f"mcr_5xx: {resp.status_code}", 502)
    if resp.status_code >= 400:
        return _err(f"mcr_{resp.status_code}", 400)
    try:
        return jsonify(resp.json()), 200
    except Exception:
        return _err("mcr_invalid_json", 502)


# ─── Import effectif (déclenche le worker async) ─────────────────────

@bp.route("/import", methods=["POST"], strict_slashes=False)
@require_auth
def trigger_import():
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    user_email = (user or {}).get("email") or ""
    if not user_sub:
        return _err("no_user_sub", 401)

    body = request.get_json(silent=True) or {}
    meeting_ids = body.get("meeting_ids") or []
    if not isinstance(meeting_ids, list) or not meeting_ids:
        return _err("meeting_ids_required", 400)
    if len(meeting_ids) > 50:
        return _err("too_many_meetings", 400)
    fallback_transcript = bool(body.get("fallback_transcript", True))

    # On a besoin d'un access_token valide AVANT d'enregistrer les rows pour
    # ne pas créer 50 lignes "pending" si le user n'est même pas authentifiable.
    try:
        _ = _get_user_access_token(user_sub)
    except (OIDCAuthError, OIDCApplicativeError, OIDCTransientError) as exc:
        return _handle_oidc_exc(exc)

    from libs.shared.app.models import UserAudioFile

    import_ids: list[str] = []
    rabbit_cfg = get_rabbit_cfg()

    db = session_scope()
    try:
        for raw_id in meeting_ids:
            mcr_meeting_id = str(raw_id)
            # Dédoublonnage : si une ligne mcr_import existe déjà pour ce user
            # et ce meeting, on la réutilise (cas typique : re-cliquer "Importer"
            # après une erreur transitoire). L'index partiel migration 019
            # garantit l'unicité côté DB.
            existing = (
                db.query(UserAudioFile)
                .filter(
                    UserAudioFile.user_sub == user_sub,
                    UserAudioFile.mcr_meeting_id == mcr_meeting_id,
                    UserAudioFile.origin == "mcr_import",
                )
                .first()
            )
            if existing:
                row_id = str(existing.id)
                # Si la précédente tentative a échoué, on relance le worker.
                if existing.transcription_status in (
                    "mcr_import_failed", "mcr_import_pending",
                ):
                    existing.transcription_status = "mcr_import_pending"
                    existing.last_activity_at = datetime.now(timezone.utc)
                    db.flush()
                    _publish_import(rabbit_cfg, row_id, mcr_meeting_id,
                                    user_sub, user_email, fallback_transcript)
                import_ids.append(row_id)
                continue

            row = UserAudioFile(
                id=uuid.uuid4(),
                user_sub=user_sub,
                user_email=user_email,
                original_session_code="MCRIMP",
                original_filename=f"mcr-meeting-{mcr_meeting_id}.webm",
                stored_filename="",  # rempli par le worker après PUT S3
                file_size_bytes=0,
                origin="mcr_import",
                transcription_status="mcr_import_pending",
                mcr_meeting_id=mcr_meeting_id,
                last_activity_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.flush()
            row_id = str(row.id)
            import_ids.append(row_id)
            _publish_import(rabbit_cfg, row_id, mcr_meeting_id,
                            user_sub, user_email, fallback_transcript)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("trigger_import: DB or queue failure")
        return _err("internal_error", 500)
    finally:
        db.close()

    return jsonify({"accepted": True, "import_ids": import_ids}), 202


def _publish_import(
    rabbit_cfg,
    user_audio_file_id: str,
    mcr_meeting_id: str,
    user_sub: str,
    user_email: str,
    fallback_transcript: bool,
) -> None:
    message = {
        "user_audio_file_id": user_audio_file_id,
        "mcr_meeting_id": mcr_meeting_id,
        "user_sub": user_sub,
        "user_email": user_email,
        "fallback_transcript": fallback_transcript,
    }
    try:
        publish_message(rabbit_cfg, QUEUE_MCR_IMPORT, message)
    except Exception:
        logger.exception(
            "trigger_import: failed to publish QUEUE_MCR_IMPORT for row=%s",
            user_audio_file_id,
        )
        raise
