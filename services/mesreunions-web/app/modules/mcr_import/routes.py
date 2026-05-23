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
        payload = _fetch_meetings_resilient(
            access_token, page=page, page_size=page_size, search=search, user_sub=user_sub,
        )
    except req.RequestException:
        logger.exception("MCR /meetings unreachable")
        return _err("mcr_unreachable", 502)
    if payload is None:
        return _err("mcr_unreachable", 502)
    if isinstance(payload, tuple):
        return _err(payload[0], payload[1])
    return jsonify(payload), 200


def _fetch_meetings_resilient(access_token, *, page, page_size, search, user_sub):
    """Liste paginée MCR avec résilience par ligne.

    Stratégie :
      1. Tente le fetch normal `page&page_size`.
      2. Si 500 avec le marker "not supported for platform" → on est dans
         le bug pydantic d'une row pourrie qui casse toute la page. Bascule
         en fallback per-row : on rappelle MCR `page_size=1` pour chaque
         position de la page demandée, on garde les 200 et on remplace les
         500 par un placeholder `_broken: true` que l'UI rendra explicitement.
      3. Tout autre status → renvoie un tuple (error_code, http_status) pour
         que le caller produise un _err propre.

    Retourne le payload paginé MCR (dict) ou un tuple (err, status) sur
    erreur non récupérable.
    """
    base = _mcr_base()

    def _call(p, ps):
        params = {"page": p, "page_size": ps}
        if search:
            params["search"] = search
        return req.get(
            f"{base}/api/meetings",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )

    resp = _call(page, page_size)
    logger.info(
        "MCR /meetings user_sub=%s status=%s page=%s size=%s body=%s",
        user_sub, resp.status_code, page, page_size, (resp.text or "")[:200],
    )
    if resp.status_code == 200:
        try:
            return resp.json()
        except Exception:
            logger.exception("MCR /meetings non-JSON")
            return ("mcr_invalid_json", 502)
    if resp.status_code in (401, 403):
        return ("mcr_forbidden", 403)
    if resp.status_code != 500 or "not supported for platform" not in (resp.text or ""):
        # Erreur que le fallback ne sait pas guérir.
        if resp.status_code >= 500:
            return (f"mcr_5xx: {resp.status_code}", 502)
        return (f"mcr_{resp.status_code}", 400)

    # ─── Fallback per-row ────────────────────────────────────────────
    # On itère sur les `page_size` slots de la page demandée. Chaque slot
    # devient une page_size=1 indépendante. Les rows qui crashent sont
    # remplacées par un placeholder lisible par l'UI.
    logger.warning(
        "MCR /meetings page=%s size=%s a planté en 500 (row pourrie). Fallback per-row.",
        page, page_size,
    )
    start_slot = (page - 1) * page_size + 1
    rows = []
    broken_count = 0
    total_items = None
    total_pages = None
    for offset in range(page_size):
        slot_page = start_slot + offset  # absolute position dans la liste MCR
        r = _call(slot_page, 1)
        body_short = (r.text or "")[:300]
        if r.status_code == 200:
            try:
                slot_payload = r.json()
            except Exception:
                logger.warning("Slot %s: 200 mais JSON invalide, on saute.", slot_page)
                continue
            if total_items is None:
                total_items = slot_payload.get("total_items")
                total_pages = slot_payload.get("total_pages")
            data = slot_payload.get("data") or []
            if not data:
                # Plus de rows à fetcher → on a atteint la fin de la liste.
                break
            rows.extend(data)
            continue
        if r.status_code == 500 and "not supported for platform" in body_short:
            broken_count += 1
            rows.append({
                "id": None,
                "name": f"⚠️ Réunion #{slot_page} impossible à charger (bug MCR)",
                "name_platform": "BROKEN",
                "status": "BROKEN",
                "creation_date": None,
                "start_date": None,
                "end_date": None,
                "url": None,
                "notes": body_short,
                "_broken": True,
                "_slot": slot_page,
                "_mcr_error": body_short,
            })
            continue
        if r.status_code in (401, 403):
            return ("mcr_forbidden", 403)
        # Erreur inattendue sur un slot → on l'expose aussi en placeholder.
        logger.warning("Slot %s: status inattendu %s — exposé en placeholder", slot_page, r.status_code)
        broken_count += 1
        rows.append({
            "id": None,
            "name": f"⚠️ Réunion #{slot_page} : erreur HTTP {r.status_code}",
            "name_platform": "BROKEN",
            "status": "BROKEN",
            "_broken": True,
            "_slot": slot_page,
            "_mcr_error": body_short,
        })

    return {
        "total_items": total_items if total_items is not None else len(rows),
        "total_pages": total_pages if total_pages is not None else page,
        "page": page,
        "data": rows,
        "_fallback_used": True,
        "_broken_count": broken_count,
    }


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

    # On a besoin d'un access_token valide AVANT de publier les messages
    # pour ne pas pourrir la queue si le user n'est même pas authentifiable.
    try:
        _ = _get_user_access_token(user_sub)
    except (OIDCAuthError, OIDCApplicativeError, OIDCTransientError) as exc:
        return _handle_oidc_exc(exc)

    rabbit_cfg = get_rabbit_cfg()
    if rabbit_cfg is None:
        return _err("rabbitmq_not_configured", 500)

    # NB : on ne fait PAS d'INSERT dans user_audio_files ici. mesreunions-web
    # ne se connecte qu'à postgres-external, alors que user_audio_files vit
    # en postgres-internal. C'est l'internal-ingester (qui consomme la queue
    # et a les credentials internes) qui INSERT la ligne en début de
    # traitement. Le dédoublonnage est garanti par l'index partiel migration
    # 019 (UNIQUE (user_sub, mcr_meeting_id) WHERE origin='mcr_import').
    published = 0
    for raw_id in meeting_ids:
        mcr_meeting_id = str(raw_id)
        try:
            _publish_import(
                rabbit_cfg,
                user_audio_file_id=None,  # le worker générera l'UUID
                mcr_meeting_id=mcr_meeting_id,
                user_sub=user_sub,
                user_email=user_email,
                fallback_transcript=fallback_transcript,
            )
            published += 1
        except Exception:
            logger.exception(
                "trigger_import: publish failed for mcr_meeting_id=%s",
                mcr_meeting_id,
            )

    if published == 0:
        return _err("publish_failed", 500)

    return jsonify({
        "accepted": True,
        "published": published,
        "requested": len(meeting_ids),
    }), 202


def _publish_import(
    rabbit_cfg,
    *,
    user_audio_file_id,  # may be None — le worker créera la row
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
            "trigger_import: failed to publish QUEUE_MCR_IMPORT for mcr_id=%s",
            mcr_meeting_id,
        )
        raise
