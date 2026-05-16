"""Blueprint ``preparations`` — endpoints browser-facing ``/api/preparations/*``.

Refacto PR3 — remplace les anciens ``/api/meeting-prep/*`` :

- ``GET    /api/preparations``                — liste (alias historique ``with_counts``)
- ``POST   /api/preparations``                — création (wizard LLM + Drive)
- ``GET    /api/preparations/<id>``           — détail
- ``PUT    /api/preparations/<id>``           — alias amend (idempotent)
- ``DELETE /api/preparations/<id>``           — soft-delete (corbeille)
- ``POST   /api/preparations/<id>/restore``   — restaure depuis corbeille
- ``DELETE /api/preparations/<id>/permanently`` — hard-delete
- ``POST   /api/preparations/<id>/amend``     — édition contenu (JSON)
- ``POST   /api/preparations/<id>/rename``    — renomme titre
- ``POST   /api/preparations/<id>/link-audio``— attache/détache un audio
- ``GET    /api/preparations/<id>/audio-files`` — audios liés
- ``GET    /api/preparations/<id>/series``    — chaîne de série

L'isolation cross-zone reste portée par ``device-token-authority`` : on injecte
``user_sub`` depuis la session OIDC et le backend interne contrôle l'accès.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests as req
from flask import Blueprint, jsonify, request

from ...shared import (
    get_current_user,
    require_auth,
    request_internal_device_api,
    trigger_audio_reprocess,
)
from .. import glossary as glossary_module
from . import service as prep_service

logger = logging.getLogger("mydevices_web.preparations.routes")

bp = Blueprint("preparations", __name__, url_prefix="/api/preparations")


# Whitelist des types de réunion acceptés par le wizard.
_ALLOWED_MEETING_TYPES = frozenset({
    "general", "one_on_one", "project_update", "steering_committee", "brainstorm",
})


def _meeting_prep_module():
    """Import lazy de ``app.meeting_prep`` (évite cycle si réorg ultérieure)."""
    from app import meeting_prep as _mp
    return _mp


def _drive_sync_module():
    try:
        from .. import drive_sync as _ds
        return _ds
    except Exception:
        return None


def _err(reason: dict | str, status: int):
    if isinstance(reason, str):
        return jsonify({"error": reason}), status
    return jsonify(reason), status


# ─── Liste / création ────────────────────────────────────────────────

@bp.route("", methods=["GET"], strict_slashes=False)
@require_auth
def list_preparations():
    """Liste les préparations actives. Option ``?with_counts=true``."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    with_counts = (request.args.get("with_counts") or "").lower() in ("1", "true", "yes")
    try:
        data = prep_service.list_preparations(user_sub, with_counts=with_counts)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "list_failed"}, status)
    preps = data.get("preparations", [])
    if with_counts:
        return jsonify({
            "preparations": preps,
            "older_than_90d_unlinked_count": int(
                data.get("older_than_90d_unlinked_count") or 0
            ),
        })
    return jsonify({"preparations": preps})


@bp.route("", methods=["POST"], strict_slashes=False)
@require_auth
def create_preparation():
    """Wizard : génère le brief (LLM ± Drive) puis le persiste."""
    # Import lazy pour ne pas durcir la dépendance à la racine.
    from libs.shared.app.config import (
        DRIVE_BASE_URL, LITELLM_API_KEY, LITELLM_BASE_URL, LLM_HTTP_TIMEOUT_SECONDS,
        LLM_MODEL_MEDIUM, OIDC_OFFLINE_ACCESS, OIDC_TOKEN_ENDPOINT,
    )
    from libs.shared.app.oidc_refresh_store import fetch_ciphertext
    from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
    from app.main import oidc_cfg

    _mp = _meeting_prep_module()

    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""

    payload = request.get_json(silent=True) or {}
    subject = (payload.get("subject") or "").strip()
    folder_raw = (payload.get("drive_folder") or "").strip()

    # Pré-conditions config (LLM + éventuellement Drive).
    if not LITELLM_BASE_URL or not LITELLM_API_KEY:
        return _err("Le LLM (LiteLLM) n'est pas configuré côté serveur.", 503)
    if folder_raw:
        if not OIDC_OFFLINE_ACCESS:
            return _err("Le mode hors-ligne OIDC est désactivé : aucun refresh token n'est conservé.", 503)
        if not DRIVE_BASE_URL:
            return _err("DRIVE_BASE_URL n'est pas configuré côté serveur.", 503)
        if not OIDC_TOKEN_ENDPOINT:
            return _err("OIDC_TOKEN_ENDPOINT n'est pas configuré côté serveur.", 503)

    role_viewpoint = (payload.get("role") or "").strip()
    expectation = (payload.get("expectation") or "").strip()
    duration_raw = payload.get("duration_minutes")
    focus_raw = payload.get("focus") or []
    meeting_type_raw = (payload.get("meeting_type") or "").strip().lower()

    if not subject:
        return _err("Le sujet de la réunion est requis.", 400)

    folder_id: "str | None" = None
    if folder_raw:
        folder_id = _mp.extract_folder_id(folder_raw)
        if not folder_id:
            return _err("Identifiant de dossier Drive invalide.", 400)
    if not role_viewpoint:
        return _err("Le rôle dans la réunion est requis.", 400)
    if not expectation:
        return _err("L'attente principale est requise.", 400)
    try:
        duration_minutes = int(duration_raw)
    except (TypeError, ValueError):
        return _err("La durée doit être un nombre entier de minutes.", 400)
    if duration_minutes <= 0 or duration_minutes > 600:
        return _err("La durée doit être comprise entre 1 et 600 minutes.", 400)
    if not isinstance(focus_raw, list):
        return _err("Le champ focus doit être une liste.", 400)
    focus_areas = [str(x).strip() for x in focus_raw if str(x).strip()]

    series_parent_id = (payload.get("series_parent_id") or "").strip() or None
    target_meeting_date = (payload.get("target_meeting_date") or "").strip() or None

    meeting_type = (
        meeting_type_raw
        if meeting_type_raw in _ALLOWED_MEETING_TYPES
        else _mp.DEFAULT_MEETING_TYPE
    )

    corpus_text = ""
    used: list = []
    if folder_id:
        ciphertext = fetch_ciphertext(user_sub)
        if not ciphertext:
            return _err({
                "error": "Aucun token Drive enregistré. Déconnectez-vous puis reconnectez-vous pour réautoriser l'accès au Drive.",
                "code": "no_refresh_token",
            }, 401)
        try:
            refresh_token = decrypt_secret(ciphertext)
        except Exception:
            logger.exception("preparations: failed to decrypt refresh token for sub=%s", user_sub)
            return _err("Token Drive illisible côté serveur.", 500)

        drive = _mp.DriveClient(
            base_url=DRIVE_BASE_URL,
            oidc_token_endpoint=OIDC_TOKEN_ENDPOINT,
            oidc_client_id=oidc_cfg.client_id,
            oidc_client_secret=oidc_cfg.client_secret,
        )

        try:
            access_token = drive.exchange_refresh(refresh_token)
        except _mp.DriveAuthError:
            logger.warning("preparations: refresh rejected by Keycloak for sub=%s", user_sub)
            return _err({
                "error": "Le jeton Drive a expiré. Déconnectez-vous puis reconnectez-vous.",
                "code": "refresh_rejected",
            }, 401)
        except _mp.DriveTransientError as exc:
            logger.warning("preparations: Keycloak transient on token exchange: %s", exc)
            return _err("Le service d'identité est temporairement indisponible.", 502)

        try:
            corpus_text, used = _mp.assemble_corpus(drive, access_token, folder_id)
        except _mp.DriveAuthError as exc:
            status_code = getattr(exc, "status_code", None)
            logger.warning(
                "preparations: Drive auth error on folder %s (status=%s): %s",
                folder_id, status_code, exc,
            )
            if status_code == 403:
                return _err({
                    "error": "Vous n'avez pas accès à ce dossier sur le Drive. Vérifiez l'URL collée ou demandez l'accès au propriétaire.",
                    "code": "drive_forbidden",
                }, 403)
            return _err({
                "error": "Accès Drive refusé. Déconnectez-vous puis reconnectez-vous.",
                "code": "drive_auth",
            }, 401)
        except _mp.DriveApplicativeError as exc:
            logger.info("preparations: Drive applicative error on folder %s: %s", folder_id, exc)
            return _err({
                "error": "Dossier Drive introuvable. Vérifiez l'URL ou l'identifiant collé.",
                "code": "drive_not_found",
            }, 404)
        except _mp.DriveTransientError as exc:
            logger.warning("preparations: Drive transient error on folder %s: %s", folder_id, exc)
            return _err("Le Drive est temporairement indisponible.", 502)

    try:
        template_text = _mp.load_prompt_template(
            _mp.prompt_path_for_type(meeting_type)
        )
    except Exception:
        logger.exception("preparations: failed to load prompt template (type=%s)", meeting_type)
        return _err("Modèle de prompt indisponible.", 500)

    # Chaînage série : key_points du dernier audio lié au parent.
    prior_key_points_text = ""
    if series_parent_id:
        try:
            audios_resp = request_internal_device_api(
                "GET",
                f"/api/v1/preparations/{series_parent_id}/audio-files",
                params={"user_sub": user_sub},
            )
            for r in (audios_resp or {}).get("audio_files") or []:
                kp = (r.get("key_points_summary") or "").strip()
                if kp:
                    prior_key_points_text = kp
                    break
        except Exception:
            logger.exception(
                "preparations: failed to fetch prior key_points for series_parent_id=%s",
                series_parent_id,
            )

    prompt = _mp.build_prompt(
        template_text,
        objective=subject,
        duration_minutes=duration_minutes,
        role_viewpoint=role_viewpoint,
        expectation=expectation,
        focus_areas=focus_areas,
        prep_docs_text=corpus_text,
        prior_key_points_text=prior_key_points_text,
    )

    llm = _mp.LLMClient(
        base_url=LITELLM_BASE_URL,
        api_key=LITELLM_API_KEY,
        timeout=LLM_HTTP_TIMEOUT_SECONDS,
    )
    try:
        brief = llm.chat_json(
            model=LLM_MODEL_MEDIUM,
            messages=[{"role": "user", "content": prompt}],
        )
    except _mp.LLMAuthError:
        logger.exception("preparations: LiteLLM auth failed")
        return _err("Le service LLM a refusé la requête (clé invalide).", 502)
    except _mp.LLMTransientError as exc:
        logger.warning("preparations: LiteLLM transient: %s", exc)
        return _err("Le service LLM est temporairement indisponible.", 502)
    except _mp.LLMApplicativeError as exc:
        logger.warning("preparations: LiteLLM applicative error: %s", exc)
        return _err("Le LLM n'a pas pu produire un brief exploitable.", 502)

    if isinstance(brief, dict):
        meta = brief.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        meta["meeting_type"] = meeting_type
        brief["_meta"] = meta

    preparation_id = None
    try:
        created = prep_service.create_preparation({
            "user_sub": user_sub,
            "subject": subject,
            "drive_folder_id": folder_id,
            "role": role_viewpoint,
            "expectation": expectation,
            "focus": focus_areas,
            "duration_minutes": duration_minutes,
            "content": brief,
            "documents": used,
            "title": subject,
            "series_parent_id": series_parent_id,
            "target_meeting_date": target_meeting_date,
        })
        preparation_id = (created.get("preparation") or {}).get("id")

        # Glossaire utilisateur global (best-effort).
        if preparation_id:
            terms = glossary_module.extract_terms_from_brief(brief, used)
            glossary_module.upsert_terms_for_user(
                user_sub, terms, source_preparation_id=preparation_id,
            )
    except Exception:
        logger.exception("preparations: failed to persist preparation for sub=%s", user_sub)

    # Versement Drive en arrière-plan (best-effort).
    if preparation_id:
        try:
            from ...drive_brief_sync import schedule_drive_brief_sync
            schedule_drive_brief_sync(
                user_sub, preparation_id, brief, used, prompt,
                drive_folder_id=folder_id,
            )
        except Exception:
            logger.exception("preparations: schedule_drive_brief_sync raised")

    return jsonify({
        "brief": brief,
        "documents": used,
        "preparation_id": preparation_id,
        "meeting_type": meeting_type,
    })


# ─── Lecture détail / mutations ──────────────────────────────────────

@bp.route("/<preparation_id>", methods=["GET"])
@require_auth
def get_preparation(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        data = prep_service.get_preparation(user_sub, preparation_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        try:
            body = err.response.json() if err.response is not None else {}
        except Exception:
            body = {}
        return _err({"error": body.get("error", "get_failed")}, status)
    prep = data.get("preparation") or {}
    # Le front lit historiquement `brief.brief_json` et `brief.title` —
    # on expose `preparation` (canonique) + `brief` (alias avec mirror
    # `content` → `brief_json`) pour ne pas casser le rendu détail.
    brief_legacy = dict(prep)
    if "content" in brief_legacy and "brief_json" not in brief_legacy:
        brief_legacy["brief_json"] = brief_legacy["content"]
    return jsonify({"preparation": prep, "brief": brief_legacy})


@bp.route("/<preparation_id>", methods=["PUT"])
@require_auth
def update_preparation(preparation_id: str):
    """Alias PUT pour l'amend (UX REST plus standard)."""
    return _amend_impl(preparation_id)


@bp.route("/<preparation_id>/amend", methods=["POST"])
@require_auth
def amend_preparation(preparation_id: str):
    return _amend_impl(preparation_id)


def _amend_impl(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    new_content = payload.get("content")
    if not isinstance(new_content, dict):
        return _err("content must be an object", 400)
    try:
        data = prep_service.amend_preparation(user_sub, preparation_id, new_content)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "amend_failed"}, status)
    return jsonify(data)


@bp.route("/<preparation_id>/rename", methods=["POST"])
@require_auth
def rename_preparation(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    new_title = (payload.get("title") or "").strip()
    if not new_title:
        return _err("title is required", 400)
    if len(new_title) > 120:
        return _err("title too long", 400)
    try:
        data = prep_service.rename_preparation(user_sub, preparation_id, new_title)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "rename_failed"}, status)
    return jsonify({"ok": True, "title": data.get("title", new_title)})


@bp.route("/<preparation_id>", methods=["DELETE"])
@require_auth
def trash_preparation(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        prep_service.trash_preparation(user_sub, preparation_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "delete_failed"}, status)
    return jsonify({"ok": True, "trashed": True})


@bp.route("/<preparation_id>/restore", methods=["POST"])
@require_auth
def restore_preparation(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        prep_service.restore_preparation(user_sub, preparation_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "restore_failed"}, status)
    return jsonify({"ok": True, "restored": True})


@bp.route("/<preparation_id>/permanently", methods=["DELETE"])
@require_auth
def hard_delete_preparation(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        prep_service.hard_delete_preparation(user_sub, preparation_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "delete_failed"}, status)
    return jsonify({"ok": True, "deleted": True})


@bp.route("/<preparation_id>/audio-files", methods=["GET"])
@require_auth
def preparation_audio_files(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        return jsonify(prep_service.audio_files_for(user_sub, preparation_id))
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "fetch_failed"}, status)


@bp.route("/<preparation_id>/series", methods=["GET"])
@require_auth
def preparation_series(preparation_id: str):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        return jsonify(prep_service.series_for(user_sub, preparation_id))
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "fetch_failed"}, status)


# ─── Link audio ↔ préparation (manuel) ───────────────────────────────

@bp.route("/<preparation_id>/link-audio", methods=["POST"])
@require_auth
def link_audio_to_preparation(preparation_id: str):
    """Lie un audio à cette préparation (manuel via UI).

    Body : ``{file_id: <uuid>}``. Si le lien change réellement, déclenche
    un reprocess best-effort.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    file_id = (payload.get("file_id") or "").strip()
    if not file_id:
        return _err("file_id is required", 400)
    return _set_link(user_sub, file_id, preparation_id)


@bp.route("/unlink-audio", methods=["POST"])
@require_auth
def unlink_audio_from_preparation():
    """Détache un audio de sa préparation (sans connaître la préparation).

    Body : ``{file_id: <uuid>}``. Utilisé par l'UI quand l'utilisateur retire
    un audio depuis l'onglet du fichier (où la préparation peut être inconnue
    côté client). Déclenche un reprocess best-effort si le lien change.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    file_id = (payload.get("file_id") or "").strip()
    if not file_id:
        return _err("file_id is required", 400)
    return _set_link(user_sub, file_id, None)


def _set_link(user_sub: str, file_id: str, preparation_id):
    try:
        result = request_internal_device_api(
            "POST", "/api/v1/files/by-id/link-preparation",
            json_body={
                "user_sub": user_sub,
                "file_id": file_id,
                "preparation_id": preparation_id,
            },
        )
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "link_failed"}, status)

    prev = result.get("previous_preparation_id")
    if prev != preparation_id:
        try:
            trigger_audio_reprocess(user_sub, file_id, preparation_id)
        except Exception:
            logger.exception(
                "preparations: failed to trigger reprocess for file=%s prep=%s",
                file_id, preparation_id,
            )
    return jsonify(result)


# ─── Diagnostic / Drive test ─────────────────────────────────────────

@bp.route("/test-drive", methods=["GET"])
@require_auth
def test_drive_access():
    """Diagnostic Drive (token / exchange / ping). Remplace /api/meeting-prep/test-drive."""
    from libs.shared.app.config import DRIVE_BASE_URL, OIDC_TOKEN_ENDPOINT
    from libs.shared.app.oidc_refresh_store import fetch_ciphertext
    from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
    from app.main import oidc_cfg

    _mp = _meeting_prep_module()

    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""

    result = {
        "token_stored": False,
        "exchange_ok": False,
        "drive_reachable": False,
        "drive_base_url": DRIVE_BASE_URL or None,
        "error": None,
    }

    ciphertext = fetch_ciphertext(user_sub)
    if not ciphertext:
        result["error"] = "Aucun refresh token enregistré. Déconnectez-vous puis reconnectez-vous."
        return jsonify(result), 200
    result["token_stored"] = True

    try:
        refresh_token = decrypt_secret(ciphertext)
    except Exception:
        logger.exception("test-drive: failed to decrypt refresh token for sub=%s", user_sub)
        result["error"] = "Refresh token illisible (clé Fernet absente côté serveur ?)."
        return jsonify(result), 200

    if not DRIVE_BASE_URL or not OIDC_TOKEN_ENDPOINT:
        result["error"] = "DRIVE_BASE_URL ou OIDC_TOKEN_ENDPOINT manquant côté serveur."
        return jsonify(result), 200

    drive = _mp.DriveClient(
        base_url=DRIVE_BASE_URL,
        oidc_token_endpoint=OIDC_TOKEN_ENDPOINT,
        oidc_client_id=oidc_cfg.client_id,
        oidc_client_secret=oidc_cfg.client_secret,
    )
    try:
        access_token = drive.exchange_refresh(refresh_token)
        result["exchange_ok"] = True
    except _mp.DriveAuthError as exc:
        result["error"] = f"Échange refresh→access refusé : {exc}"
        return jsonify(result), 200
    except _mp.DriveTransientError as exc:
        result["error"] = f"Keycloak temporairement indisponible : {exc}"
        return jsonify(result), 200

    try:
        resp = req.get(
            DRIVE_BASE_URL.rstrip("/") + "/api/v1.0/items/",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        result["drive_status_code"] = resp.status_code
        if 200 <= resp.status_code < 300:
            result["drive_reachable"] = True
        elif resp.status_code in (401, 403):
            result["error"] = (
                f"Drive a refusé le token (HTTP {resp.status_code}). "
                "Le Drive et le SSO partagent-ils bien le même realm Keycloak ?"
            )
        else:
            result["error"] = f"Drive HTTP {resp.status_code}"
    except Exception as exc:
        logger.warning("test-drive: drive ping failed: %s", exc)
        result["error"] = f"Drive injoignable : {exc}"

    return jsonify(result), 200


# ─── Auto-link suggestion (diagnostic) ──────────────────────────────

@bp.route("/link-suggestion", methods=["GET"])
@require_auth
def preparation_link_suggestion():
    """Top-3 candidats pour un audio (diagnostic auto-link)."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    file_id = (request.args.get("file_id") or "").strip()
    if not file_id:
        return _err("file_id required", 400)

    try:
        files_resp = request_internal_device_api(
            "GET", "/api/v1/files/by-id",
            params={"user_sub": user_sub, "file_id": file_id},
        )
        audio = files_resp.get("file") or files_resp
    except Exception:
        audio = None
    try:
        preps_resp = request_internal_device_api(
            "GET", "/api/v1/preparations",
            params={"user_sub": user_sub, "limit": 50},
        )
        preps = preps_resp.get("preparations") or []
    except Exception:
        preps = []

    fname = (audio or {}).get("original_filename") or ""
    upload_at_iso = (audio or {}).get("created_at")
    upload_at = None
    if upload_at_iso:
        try:
            upload_at = datetime.fromisoformat(upload_at_iso.replace("Z", "+00:00"))
        except Exception:
            upload_at = None

    scored = []
    for p in preps:
        score, breakdown = _score_preparation_candidate(p, fname, upload_at)
        scored.append({
            "preparation_id": p.get("id"),
            "title": p.get("title") or p.get("subject"),
            "score": round(score, 3),
            "breakdown": breakdown,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return jsonify({"top": scored[:3]})


def _score_preparation_candidate(prep: dict, audio_filename: str, audio_upload_at):
    """Mini-scoring multi-signaux (similarité titre + proximité temporelle)."""
    import re

    def _tokens(s):
        if not s:
            return set()
        return {t for t in re.findall(r"[a-z0-9éèêàâïôûç]{3,}", s.lower()) if t}

    pcontent = prep.get("content") or {}
    psubj = (prep.get("subject") or prep.get("title") or "") + " " + (
        (pcontent or {}).get("objective_reformulated") or ""
    )
    a_tokens = _tokens(audio_filename)
    p_tokens = _tokens(psubj)
    union = a_tokens | p_tokens
    sim = (len(a_tokens & p_tokens) / len(union)) if union else 0.0

    prox = 0.0
    p_created_at = prep.get("created_at")
    if p_created_at and audio_upload_at:
        try:
            bc = datetime.fromisoformat(p_created_at.replace("Z", "+00:00"))
            delta_hours = abs((audio_upload_at - bc).total_seconds()) / 3600.0
            if delta_hours <= 24:
                prox = 1.0
            elif delta_hours >= 24 * 14:
                prox = 0.0
            else:
                prox = 1.0 - (delta_hours - 24) / (24 * 14 - 24)
        except Exception:
            prox = 0.0

    drive_loc = 0.0
    folder = prep.get("drive_folder_id") or ""
    if folder and audio_filename:
        common = 0
        for ca, cb in zip(audio_filename.lower(), folder.lower()):
            if ca == cb:
                common += 1
            else:
                break
        if common >= 4:
            drive_loc = 1.0

    engagement = 0.0
    lv = prep.get("last_viewed_at")
    if lv:
        try:
            lvt = datetime.fromisoformat(lv.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if (now - lvt).total_seconds() <= 24 * 3600:
                engagement = 1.0
        except Exception:
            engagement = 0.0

    anti_rebound = 1.0
    score = 0.30 * sim + 0.40 * prox + 0.10 * drive_loc + 0.10 * engagement + 0.10 * anti_rebound
    return score, {
        "similarity": round(sim, 3),
        "temporal": round(prox, 3),
        "drive_colocation": round(drive_loc, 3),
        "engagement": round(engagement, 3),
        "anti_rebound": round(anti_rebound, 3),
    }
