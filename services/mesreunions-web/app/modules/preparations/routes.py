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
import threading
from datetime import datetime, timezone

import requests as req
from flask import Blueprint, Response, jsonify, request

from ...shared import (
    get_current_user,
    require_auth,
    request_internal_device_api,
    trigger_audio_reprocess,
)
from .. import glossary as glossary_module
from . import exporters as prep_exporters
from . import service as prep_service
from . import generation_jobs

logger = logging.getLogger("mesreunions_web.preparations.routes")

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


@bp.route("/themes-suggestions", methods=["GET"], strict_slashes=False)
@require_auth
def list_themes_suggestions():
    """Lot 9 — Top thématiques utilisateur (proxy DTA).

    Retour : ``{themes: [{label, count}, ...]}`` (max 30, ordre desc).
    Tout échec backend → liste vide (ne casse pas l'UI wizard).
    """
    from ...shared import request_internal_preparation_api
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    try:
        data = request_internal_preparation_api(
            "GET", "/api/v1/preparations/themes-suggestions",
            params={"user_sub": user_sub},
        )
        return jsonify({"themes": (data or {}).get("themes") or []})
    except Exception:
        logger.exception("themes-suggestions proxy failed")
        return jsonify({"themes": []})


@bp.route("", methods=["POST"], strict_slashes=False)
@require_auth
def create_preparation():
    """Wizard : génère le brief (LLM ± Drive) en mode **async** (Lot 2).

    Validation + spawn d'un worker daemon, retour HTTP 202 immédiat avec
    ``{job_id}``. Le front poll ensuite ``GET /api/preparations/jobs/<job_id>``
    pour récupérer la progression (phases ``listing_docs`` → ``reading_doc`` →
    ``generating_llm`` → ``persisting`` → ``done``).

    Compat : conserve les anciens consommateurs synchrones via le query
    string ``?sync=1`` (utilisé par les tests de régression).
    """
    # Import lazy pour ne pas durcir la dépendance à la racine.
    from libs.shared.app.config import (
        DRIVE_BASE_URL, LITELLM_API_KEY, LITELLM_BASE_URL, OIDC_OFFLINE_ACCESS,
        OIDC_TOKEN_ENDPOINT,
    )

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
    # Lot 6 — récurrence éventuelle. On laisse la normalisation côté DTA
    # (recurrence.py) : ici on transmet brut (dict ou None) — le serveur
    # interne rejette/ignore si invalide.
    is_recurring_raw = payload.get("is_recurring")
    recurrence_rule_raw = payload.get("recurrence_rule")
    if not isinstance(recurrence_rule_raw, dict):
        recurrence_rule_raw = None
    # Lot 9 — thématiques additionnelles wizard (cap 50, dédup côté DTA).
    themes_raw = payload.get("themes")
    themes_clean: list[str] = []
    if isinstance(themes_raw, list):
        seen_lc: set[str] = set()
        for t in themes_raw:
            if not isinstance(t, str):
                continue
            s = t.strip()
            if not s:
                continue
            lc = s.lower()
            if lc in seen_lc:
                continue
            seen_lc.add(lc)
            themes_clean.append(s)
            if len(themes_clean) >= 50:
                break
    # Lot 8 — toggle envoi CR auto post-transcription (défaut False).
    send_cr_email = bool(payload.get("send_cr_email"))
    # Lot 5 — participants attendus saisis depuis le wizard. Liste d'objets
    # {name?, email?, role?}. Persistés en colonne JSONB côté DTA.
    participants_raw = payload.get("participants")
    participants_clean: list[dict] = []
    if isinstance(participants_raw, list):
        for raw in participants_raw:
            if not isinstance(raw, dict):
                continue
            name = (raw.get("name") or "").strip()
            email = (raw.get("email") or "").strip()
            role_ = (raw.get("role") or "").strip()
            if not (name or email):
                continue
            entry: dict = {}
            if name:
                entry["name"] = name[:200]
            if email:
                entry["email"] = email[:320]
            if role_:
                entry["role"] = role_[:120]
            participants_clean.append(entry)
        if len(participants_clean) > 100:
            participants_clean = participants_clean[:100]

    meeting_type = (
        meeting_type_raw
        if meeting_type_raw in _ALLOWED_MEETING_TYPES
        else _mp.DEFAULT_MEETING_TYPE
    )

    job_payload = {
        "user_sub": user_sub,
        "subject": subject,
        "folder_id": folder_id,
        "role_viewpoint": role_viewpoint,
        "expectation": expectation,
        "duration_minutes": duration_minutes,
        "focus_areas": focus_areas,
        "meeting_type": meeting_type,
        "series_parent_id": series_parent_id,
        "target_meeting_date": target_meeting_date,
        "participants": participants_clean,
        # Lot 6 — récurrence (transmise telle quelle au worker puis au DTA)
        "is_recurring": bool(is_recurring_raw) if is_recurring_raw is not None else None,
        "recurrence_rule": recurrence_rule_raw,
        "themes": themes_clean,
        "send_cr_email": send_cr_email,
    }

    # Mode async (par défaut, Lot 2).
    sync_mode = (request.args.get("sync") or "").lower() in ("1", "true", "yes")
    if not sync_mode:
        job_id = generation_jobs.create_job(user_sub)
        worker = threading.Thread(
            target=_run_generation_worker,
            args=(job_id, job_payload),
            name=f"prep-gen-{job_id[:8]}",
            daemon=True,
        )
        worker.start()
        return jsonify({"job_id": job_id, "status": "queued"}), 202

    # Mode sync (tests de régression).
    return _run_generation_inline(job_payload)


def _run_generation_worker(job_id: str, job: dict) -> None:
    """Worker daemon — exécute la génération + persiste l'état via generation_jobs."""
    try:
        result = _execute_generation(job, job_id=job_id)
        generation_jobs.mark_done(job_id, result.get("preparation_id"))
    except Exception as exc:
        logger.exception("preparations: generation worker failed (job=%s)", job_id)
        generation_jobs.mark_failed(job_id, str(exc))


def _run_generation_inline(job: dict):
    """Mode synchrone (compat tests) — exécute en bloc et retourne la réponse."""
    try:
        result = _execute_generation(job, job_id=None)
    except _SyncGenerationError as exc:
        return _err(exc.payload, exc.status)
    return jsonify({
        "brief": result["brief"],
        "documents": result["documents"],
        "preparation_id": result["preparation_id"],
        "meeting_type": result["meeting_type"],
    })


class _SyncGenerationError(Exception):
    def __init__(self, payload, status):
        super().__init__(str(payload))
        self.payload = payload
        self.status = status


def _execute_generation(job: dict, *, job_id: "str | None") -> dict:
    """Cœur métier — partagé entre worker async (Lot 2) et mode sync (tests).

    En mode async, met à jour ``generation_jobs`` à chaque étape. En mode
    sync, lève ``_SyncGenerationError(payload, status)`` sur échec attendu
    (validation Drive/LLM) pour que le caller renvoie l'erreur HTTP.
    """
    from libs.shared.app.config import (
        DRIVE_BASE_URL, LITELLM_API_KEY, LITELLM_BASE_URL, LLM_HTTP_TIMEOUT_SECONDS,
        LLM_MODEL_MEDIUM, OIDC_TOKEN_ENDPOINT,
    )
    from libs.shared.app.oidc_refresh_store import fetch_ciphertext
    from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
    from app.main import oidc_cfg

    _mp = _meeting_prep_module()

    user_sub = job["user_sub"]
    subject = job["subject"]
    folder_id = job["folder_id"]
    role_viewpoint = job["role_viewpoint"]
    expectation = job["expectation"]
    duration_minutes = job["duration_minutes"]
    focus_areas = job["focus_areas"]
    meeting_type = job["meeting_type"]
    series_parent_id = job["series_parent_id"]
    target_meeting_date = job["target_meeting_date"]
    participants = job.get("participants") or []
    is_recurring = job.get("is_recurring")
    recurrence_rule = job.get("recurrence_rule")
    themes = job.get("themes") or []
    send_cr_email = bool(job.get("send_cr_email"))

    def _update(**kw):
        if job_id:
            generation_jobs.update_job(job_id, **kw)

    def _fail(payload, status):
        if job_id:
            err_msg = payload if isinstance(payload, str) else (
                (payload or {}).get("error") or "generation_failed"
            )
            generation_jobs.update_job(job_id, error=err_msg)
            raise RuntimeError(err_msg)
        raise _SyncGenerationError(payload, status)

    _update(phase="init")

    corpus_text = ""
    used: list = []
    if folder_id:
        _update(phase="test_drive")
        ciphertext = fetch_ciphertext(user_sub)
        if not ciphertext:
            _fail({
                "error": "Aucun token Drive enregistré. Déconnectez-vous puis reconnectez-vous pour réautoriser l'accès au Drive.",
                "code": "no_refresh_token",
            }, 401)
        try:
            refresh_token = decrypt_secret(ciphertext)
        except Exception:
            logger.exception("preparations: failed to decrypt refresh token for sub=%s", user_sub)
            _fail("Token Drive illisible côté serveur.", 500)

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
            _fail({
                "error": "Le jeton Drive a expiré. Déconnectez-vous puis reconnectez-vous.",
                "code": "refresh_rejected",
            }, 401)
        except _mp.DriveTransientError as exc:
            logger.warning("preparations: Keycloak transient on token exchange: %s", exc)
            _fail("Le service d'identité est temporairement indisponible.", 502)

        def _progress(**kw):
            _update(**kw)

        try:
            corpus_text, used = _mp.assemble_corpus(
                drive, access_token, folder_id, progress=_progress,
            )
        except _mp.DriveAuthError as exc:
            status_code = getattr(exc, "status_code", None)
            logger.warning(
                "preparations: Drive auth error on folder %s (status=%s): %s",
                folder_id, status_code, exc,
            )
            if status_code == 403:
                _fail({
                    "error": "Vous n'avez pas accès à ce dossier sur le Drive. Vérifiez l'URL collée ou demandez l'accès au propriétaire.",
                    "code": "drive_forbidden",
                }, 403)
            _fail({
                "error": "Accès Drive refusé. Déconnectez-vous puis reconnectez-vous.",
                "code": "drive_auth",
            }, 401)
        except _mp.DriveApplicativeError as exc:
            logger.info("preparations: Drive applicative error on folder %s: %s", folder_id, exc)
            _fail({
                "error": "Dossier Drive introuvable. Vérifiez l'URL ou l'identifiant collé.",
                "code": "drive_not_found",
            }, 404)
        except _mp.DriveTransientError as exc:
            logger.warning("preparations: Drive transient error on folder %s: %s", folder_id, exc)
            _fail("Le Drive est temporairement indisponible.", 502)

    try:
        template_text = _mp.load_prompt_template(
            _mp.prompt_path_for_type(meeting_type)
        )
    except Exception:
        logger.exception("preparations: failed to load prompt template (type=%s)", meeting_type)
        _fail("Modèle de prompt indisponible.", 500)

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

    _update(phase="generating_llm")
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
        _fail("Le service LLM a refusé la requête (clé invalide).", 502)
    except _mp.LLMTransientError as exc:
        logger.warning("preparations: LiteLLM transient: %s", exc)
        _fail("Le service LLM est temporairement indisponible.", 502)
    except _mp.LLMApplicativeError as exc:
        logger.warning("preparations: LiteLLM applicative error: %s", exc)
        _fail("Le LLM n'a pas pu produire un brief exploitable.", 502)

    if isinstance(brief, dict):
        meta = brief.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        meta["meeting_type"] = meeting_type
        brief["_meta"] = meta

    _update(phase="persisting")
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
            "participants": participants,
            "is_recurring": bool(is_recurring) if is_recurring is not None else False,
            "recurrence_rule": recurrence_rule,
            "themes": themes,
            "send_cr_email": send_cr_email,
        })
        preparation_id = (created.get("preparation") or {}).get("id")

        # Glossaire utilisateur global (best-effort).
        if preparation_id:
            _update(phase="extracting_glossary", preparation_id=preparation_id)
            terms = glossary_module.extract_terms_from_brief(brief, used)
            glossary_module.upsert_terms_for_user(
                user_sub, terms, source_preparation_id=preparation_id,
            )
    except Exception:
        logger.exception("preparations: failed to persist preparation for sub=%s", user_sub)

    # Versement Drive en arrière-plan (best-effort).
    if preparation_id:
        try:
            from ..drive_sync import schedule_drive_brief_sync
            schedule_drive_brief_sync(
                user_sub, preparation_id, brief, used, prompt,
                drive_folder_id=folder_id,
            )
        except Exception:
            logger.exception("preparations: schedule_drive_brief_sync raised")

    return {
        "brief": brief,
        "documents": used,
        "preparation_id": preparation_id,
        "meeting_type": meeting_type,
    }


# ─── Job status endpoint (Lot 2 — polling animation génération) ──────

@bp.route("/jobs/<job_id>", methods=["GET"])
@require_auth
def generation_job_status(job_id: str):
    """Retourne l'état de progression d'un job de génération de brief."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    job = generation_jobs.get_job(job_id, user_sub)
    if not job:
        return jsonify({"phase": "unknown", "error": "Job introuvable ou expiré."}), 404
    return jsonify({
        "job_id": job["id"],
        "phase": job["phase"],
        "current_doc": job.get("current_doc"),
        "docs_processed": job.get("docs_processed") or 0,
        "docs_total": job.get("docs_total") or 0,
        "preparation_id": job.get("preparation_id"),
        "error": job.get("error"),
    })


@bp.route("/<preparation_id>/generation-status", methods=["GET"])
@require_auth
def preparation_generation_status_by_id(preparation_id: str):
    """Alias — quand le front a déjà l'id final (post mark_done) : 200 ``done``.

    Sert de fallback si le client a perdu le ``job_id`` (refresh, etc.).
    """
    return jsonify({
        "phase": "done",
        "preparation_id": preparation_id,
        "current_doc": None,
        "docs_processed": 0,
        "docs_total": 0,
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
    # PR4 : le front consomme `preparation.content` (clé canonique). Les
    # anciens alias `brief`/`brief_json` ont été retirés — `frontend/legacy.js`
    # est à jour.
    return jsonify({"preparation": prep})


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
    """Amend ``content`` et/ou ``participants`` et/ou ``glossary_source``.

    Lot 3/5 : on accepte désormais 3 champs optionnels mais au moins l'un
    d'eux doit être fourni. Compat : un body ``{content: {...}}`` continue
    de fonctionner comme avant.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    new_content = payload.get("content") if "content" in payload else None
    new_participants = payload.get("participants") if "participants" in payload else None
    new_glossary = payload.get("glossary_source") if "glossary_source" in payload else None
    # Lot 6 — récurrence (3 champs liés). On utilise ``in payload`` pour
    # distinguer "absent" (pas de mutation) de présent à None (effacement).
    has_recurring = "is_recurring" in payload
    has_rule = "recurrence_rule" in payload
    has_target = "target_meeting_date" in payload
    new_is_recurring = bool(payload.get("is_recurring")) if has_recurring else None
    raw_rule = payload.get("recurrence_rule") if has_rule else None
    if has_rule and raw_rule is not None and not isinstance(raw_rule, dict):
        return _err("recurrence_rule must be an object or null", 400)
    raw_target = payload.get("target_meeting_date") if has_target else None
    # Lot 8 + 9 — nouveaux champs : themes, send_cr_email,
    # drive_main_courante_doc_id (tous optionnels).
    has_themes = "themes" in payload
    has_send_cr = "send_cr_email" in payload
    has_main_courante = "drive_main_courante_doc_id" in payload
    new_themes = payload.get("themes") if has_themes else None
    new_send_cr = bool(payload.get("send_cr_email")) if has_send_cr else None
    raw_main_courante = payload.get("drive_main_courante_doc_id") if has_main_courante else None

    if (new_content is None and new_participants is None and new_glossary is None
            and not has_recurring and not has_rule and not has_target
            and not has_themes and not has_send_cr and not has_main_courante):
        return _err("content, participants, glossary_source, recurrence, themes or send_cr_email required", 400)
    if new_content is not None and not isinstance(new_content, dict):
        return _err("content must be an object", 400)
    if new_participants is not None and not isinstance(new_participants, list):
        return _err("participants must be a list", 400)
    if new_glossary is not None and not isinstance(new_glossary, list):
        return _err("glossary_source must be a list", 400)
    if has_themes and not isinstance(new_themes, list):
        return _err("themes must be a list", 400)
    if has_themes:
        # Cap 50 + dédoublonnage côté gateway (DTA refait le ménage aussi).
        cleaned_themes: list[str] = []
        seen_lc: set[str] = set()
        for t in new_themes:
            if not isinstance(t, str):
                continue
            s = t.strip()
            if not s:
                continue
            lc = s.lower()
            if lc in seen_lc:
                continue
            seen_lc.add(lc)
            cleaned_themes.append(s)
            if len(cleaned_themes) >= 50:
                break
        new_themes = cleaned_themes

    try:
        # Sentinels du module service : utiliser leurs valeurs propres pour
        # signaler "non fourni" (object identity-based discrimination).
        kw_recur: dict = {}
        if has_rule:
            kw_recur["recurrence_rule"] = raw_rule
        if has_target:
            kw_recur["target_meeting_date"] = raw_target
        if has_main_courante:
            kw_recur["drive_main_courante_doc_id"] = raw_main_courante
        data = prep_service.amend_preparation(
            user_sub, preparation_id, new_content,
            participants=new_participants, glossary_source=new_glossary,
            is_recurring=new_is_recurring,
            themes=new_themes if has_themes else None,
            send_cr_email=new_send_cr,
            **kw_recur,
        )
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "amend_failed"}, status)
    return jsonify(data)


# ─── Lot 3c — Glossaire (lecture/écriture + push global) ─────────────

@bp.route("/<preparation_id>/glossary", methods=["POST"])
@require_auth
def update_preparation_glossary(preparation_id: str):
    """Met à jour le glossaire d'une préparation (Lot 3c).

    Body : ``{terms: [{term, definition?, global?: bool}, ...]}``.

    - Persiste l'intégralité de la liste dans ``preparation.glossary_source``
      (remplacement, pas d'append) — ordre conservé.
    - Pour les termes cochés ``global: true``, proxy vers
      ``/api/v1/user-glossary/upsert-batch`` (best-effort, échecs silencieux).
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    terms_raw = payload.get("terms")
    if not isinstance(terms_raw, list):
        return _err("terms must be a list", 400)

    cleaned: list[dict] = []
    globals_to_push: list[dict] = []
    for raw in terms_raw:
        if not isinstance(raw, dict):
            continue
        term = (raw.get("term") or "").strip()
        if not term:
            continue
        if len(term) > 200:
            term = term[:200]
        definition = (raw.get("definition") or "").strip() or None
        is_global = bool(raw.get("global") or raw.get("is_global"))
        entry = {"term": term}
        if definition:
            entry["definition"] = definition
        if is_global:
            entry["global"] = True
        cleaned.append(entry)
        if is_global:
            push_entry = {"term": term}
            if definition:
                push_entry["definition"] = definition
            globals_to_push.append(push_entry)

    # Cap raisonnable côté serveur : 300 termes max (cf. memo)
    if len(cleaned) > 300:
        cleaned = cleaned[:300]

    try:
        data = prep_service.amend_preparation(
            user_sub, preparation_id, None, glossary_source=cleaned,
        )
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "glossary_save_failed"}, status)

    # Best-effort push vers user_glossary_terms.
    pushed = 0
    if globals_to_push:
        try:
            glossary_module.upsert_terms_for_user(
                user_sub, globals_to_push, source_preparation_id=preparation_id,
            )
            pushed = len(globals_to_push)
        except Exception:
            logger.exception(
                "preparations: failed to push global glossary terms for prep=%s",
                preparation_id,
            )
    return jsonify({
        "ok": True,
        "terms_count": len(cleaned),
        "globals_pushed": pushed,
        "preparation": (data or {}).get("preparation"),
    })


@bp.route("/<preparation_id>/participants", methods=["POST"])
@require_auth
def update_preparation_participants(preparation_id: str):
    """Met à jour la liste des participants (Lot 5).

    Body : ``{participants: [{name, email?, role?}, ...]}``. Persiste dans
    la colonne ``participants`` (JSONB) via l'endpoint amend étendu.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    payload = request.get_json(silent=True) or {}
    participants_raw = payload.get("participants")
    if not isinstance(participants_raw, list):
        return _err("participants must be a list", 400)

    cleaned: list[dict] = []
    for raw in participants_raw:
        if not isinstance(raw, dict):
            continue
        name = (raw.get("name") or "").strip()
        email = (raw.get("email") or "").strip()
        role = (raw.get("role") or "").strip()
        if not (name or email):
            continue
        entry: dict = {}
        if name:
            entry["name"] = name[:200]
        if email:
            entry["email"] = email[:320]
        if role:
            entry["role"] = role[:120]
        cleaned.append(entry)

    if len(cleaned) > 100:
        cleaned = cleaned[:100]

    try:
        data = prep_service.amend_preparation(
            user_sub, preparation_id, None, participants=cleaned,
        )
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        return _err({"error": "participants_save_failed"}, status)
    return jsonify({
        "ok": True,
        "participants_count": len(cleaned),
        "preparation": (data or {}).get("preparation"),
    })


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


# ─── Lot 4 — Export DOCX / ODT (TXT + MD générés côté front) ────────

@bp.route("/<preparation_id>/export", methods=["GET"])
@require_auth
def export_preparation(preparation_id: str):
    """Exporte une préparation en ``?format=docx|odt`` (binaire).

    TXT et MD sont générés côté front (cf. ``frontend/lib/export-formatter.js``)
    via sérialisation directe de ``preparation.content`` — pas de round-trip
    réseau utile pour ces formats texte. Ici on ne traite que les formats
    binaires nécessitant python-docx / odfpy.
    """
    fmt = (request.args.get("format") or "").lower().strip()
    if fmt not in ("docx", "odt"):
        return _err("format must be 'docx' or 'odt'", 400)

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
    if not prep:
        return _err("preparation_not_found", 404)

    try:
        payload, content_type, filename = prep_exporters.render(prep, fmt)
    except ValueError as e:
        return _err(str(e), 400)
    except Exception:
        logger.exception("export render failed (prep=%s fmt=%s)", preparation_id, fmt)
        return _err("export_failed", 500)

    # RFC 5987 : filename* en plus du filename ASCII (slugifié) pour le
    # support des caractères non-ASCII si on retire le slugify un jour.
    headers = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(payload)),
        "Cache-Control": "no-store",
    }
    return Response(payload, status=200, headers=headers)


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
    """Diagnostic Drive (token / exchange / ping). Remplace /api/meeting-prep/test-drive.

    Extension Lot 1 : si ``?folder_id=...`` (ou une URL Drive) est fourni,
    on liste le contenu du dossier et on renvoie ``{ok, docs_count, docs}``
    pour permettre à l'UI d'afficher "N documents trouvés".
    """
    from libs.shared.app.config import DRIVE_BASE_URL, OIDC_TOKEN_ENDPOINT
    from libs.shared.app.oidc_refresh_store import fetch_ciphertext
    from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
    from app.main import oidc_cfg

    _mp = _meeting_prep_module()

    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""

    folder_raw = (request.args.get("folder_id") or request.args.get("folder") or "").strip()
    folder_id = _mp.extract_folder_id(folder_raw) if folder_raw else None

    result = {
        "token_stored": False,
        "exchange_ok": False,
        "drive_reachable": False,
        "drive_base_url": DRIVE_BASE_URL or None,
        "error": None,
    }
    if folder_raw:
        result["folder_id"] = folder_id
        result["ok"] = False
        result["docs_count"] = 0
        result["docs"] = []
        if not folder_id:
            result["error"] = "Identifiant ou URL de dossier Drive invalide."
            return jsonify(result), 200

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

    # Si folder_id fourni → on tente directement le listing du dossier.
    if folder_id:
        try:
            children = drive.list_children(access_token, folder_id)
        except _mp.DriveAuthError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 403:
                result["error"] = (
                    "Vous n'avez pas accès à ce dossier. Vérifiez l'URL "
                    "collée ou demandez l'accès au propriétaire."
                )
            else:
                result["error"] = "Drive a refusé le jeton. Reconnectez-vous."
            return jsonify(result), 200
        except _mp.DriveApplicativeError:
            result["error"] = (
                "Dossier introuvable. Vérifiez l'URL ou l'identifiant collé."
            )
            return jsonify(result), 200
        except _mp.DriveTransientError as exc:
            result["error"] = f"Drive temporairement indisponible : {exc}"
            return jsonify(result), 200
        except Exception as exc:
            logger.warning("test-drive: list_children failed: %s", exc)
            result["error"] = f"Erreur listing Drive : {exc}"
            return jsonify(result), 200

        docs = []
        for item in (children or []):
            kind = (item.get("type") or item.get("kind") or "").lower()
            is_folder = kind in {"folder", "directory"} or bool(item.get("is_folder"))
            name = (
                item.get("title") or item.get("name") or item.get("filename")
                or item.get("id") or "(sans nom)"
            )
            docs.append({
                "name": name,
                "id": item.get("id") or "",
                "is_folder": is_folder,
                "mime_type": item.get("mime_type") or item.get("mimetype") or None,
                "size": item.get("size"),
            })
        leaf_docs = [d for d in docs if not d["is_folder"]]
        result["drive_reachable"] = True
        result["ok"] = True
        result["docs_count"] = len(leaf_docs)
        result["docs"] = docs
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
