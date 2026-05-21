"""Routes feedback — pouce ↑/↓ + regénération.

Toutes les routes proxy vers internal-ingester (où vit la table
user_feedback côté postgres-internal). Voir migration 015 +
``services/dmz-to-internal-bridge/app/puller.py`` pour les endpoints
``/api/v1/feedback*``.
"""
from __future__ import annotations

import logging
import os
import sys

import requests as req
from flask import Blueprint, jsonify, request

# Imports legacy "à la racine" — pattern hérité des autres modules.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from libs.shared.app.config import INTERNAL_API_TOKEN  # noqa: E402
from libs.shared.app.database import with_db_retry  # noqa: E402

from app.shared import get_current_user, require_auth  # noqa: E402
from app.runtime import session_scope  # noqa: E402
from app.modules.sessions import service as sess_svc  # noqa: E402


bp = Blueprint("feedback", __name__)
logger = logging.getLogger("mesreunions_web.feedback")


def _ingester_base() -> str:
    """URL du service internal-ingester (port 8090 par défaut)."""
    return os.getenv(
        "FILE_PULLER_INTERNAL_BASE_URL",
        "http://internal-ingester:8090",
    ).rstrip("/")


def _call_ingester(method: str, path: str, *, json_body=None, params=None, timeout: int = 10):
    """Appel HTTP authentifié vers internal-ingester. Lève HTTPError ≥ 400."""
    url = f"{_ingester_base()}{path}"
    resp = req.request(
        method, url,
        json=json_body, params=params,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    return resp


def _is_admin(user) -> bool:
    """Check claim admin sur l'utilisateur courant.

    Aligné avec frontend/lib/auth.js isAdmin() : roles[].lowercase() === 'admin'.
    """
    if not user:
        return False
    roles = user.get("roles") or []
    if not isinstance(roles, list):
        return False
    return any(str(r).lower() == "admin" for r in roles)


def _ensure_admin_or_403(user):
    """Retourne une Response 403 si l'utilisateur n'est pas admin, sinon None."""
    if not _is_admin(user):
        return jsonify({"error": "admin_required"}), 403
    return None


def _resolve_internal_audio_id(user_sub: str, external_file_id: str) -> str | None:
    """Mappe un file_id externe (uploaded_files.id, table external) vers
    l'audio.id interne (user_audio_files.id, table internal) via le lookup
    déjà utilisé par sessions/routes.py.

    Critique pour les endpoints qui parlent à internal-ingester avec un
    audio_id : sans ce mapping, l'ingester cherche user_audio_files.id ==
    external_file_id et retourne 404 (les 2 tables ont des UUIDs distincts).

    Retourne None si le fichier n'existe pas ou n'a pas encore d'audio
    associé (transcription pas encore démarrée).

    Wrapped in with_db_retry pour absorber les "server closed the
    connection unexpectedly" sporadiques causés par un routing
    inter-cluster SCW LB déficient (~50% fail rate depuis internal-gw
    vers postgres-external-lb via PN, alors que 0% depuis external-gw).
    3 essais suffisent à ramener le taux d'échec utilisateur à <0.5%
    (= cas où les 3 essais consécutifs touchent une mauvaise route).
    """
    def _do_lookup():
        db = session_scope()
        try:
            file_obj = sess_svc.get_owned_file(db, user_sub, external_file_id)
            if not file_obj:
                return None
            audio = sess_svc.lookup_audio_outputs(db, file_obj)
            if not audio:
                return None
            return audio.get("id")
        finally:
            try: db.close()
            except Exception: pass
    return with_db_retry(_do_lookup, max_attempts=3)


# ─── POST feedback (utilisateur lambda) ─────────────────────────────


@bp.route("/api/file/<file_id>/feedback", methods=["POST"])
@require_auth
def create_file_feedback(file_id: str):
    """Body : {type: 'usefulness'|'regenerate', payload: dict}.

    file_id passe en payload.file_id côté ingester. Le user_sub vient
    automatiquement de la session OIDC.
    """
    return _create_feedback_impl(file_id=file_id)


@bp.route("/api/feedback", methods=["POST"])
@require_auth
def create_global_feedback():
    """Feedback non rattaché à une réunion (ex: feedback global app)."""
    return _create_feedback_impl(file_id=None)


def _create_feedback_impl(*, file_id):
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    fb_type = (body.get("type") or "").strip()
    if fb_type not in ("usefulness", "regenerate"):
        return jsonify({"error": "type must be 'usefulness' or 'regenerate'"}), 400
    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "payload must be a JSON object"}), 400
    try:
        resp = _call_ingester("POST", "/api/v1/feedback", json_body={
            "user_sub": user_sub,
            "file_id": file_id,
            "type": fb_type,
            "payload": payload,
        })
    except req.RequestException:
        logger.exception("create_feedback: ingester unreachable")
        return jsonify({"error": "ingester_unavailable"}), 502
    if resp.status_code >= 400:
        return jsonify(resp.json() if resp.content else {"error": "ingester_error"}), resp.status_code
    return jsonify(resp.json()), 201


# ─── GET mes feedbacks (utilisateur) ─────────────────────────────────


@bp.route("/api/my-feedback", methods=["GET"])
@require_auth
def list_my_feedback():
    """Liste paginée des feedbacks de l'utilisateur courant. ?limit=&offset=."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    params = {
        "user_sub": user_sub,
        "limit": request.args.get("limit", 50),
        "offset": request.args.get("offset", 0),
    }
    try:
        resp = _call_ingester("GET", "/api/v1/feedback/mine", params=params)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    if resp.status_code >= 400:
        return jsonify(resp.json() if resp.content else {"error": "ingester_error"}), resp.status_code
    return jsonify(resp.json())


# ─── Admin : list + patch ────────────────────────────────────────────


@bp.route("/api/admin/feedback", methods=["GET"])
@require_auth
def list_admin_feedback():
    """Liste paginée de TOUS les feedbacks (admin only). ?status=&limit=&offset=."""
    err = _ensure_admin_or_403(get_current_user())
    if err is not None:
        return err
    params = {}
    if request.args.get("status"):
        params["status"] = request.args["status"]
    params["limit"] = request.args.get("limit", 100)
    params["offset"] = request.args.get("offset", 0)
    try:
        resp = _call_ingester("GET", "/api/v1/feedback/all", params=params)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    if resp.status_code >= 400:
        return jsonify(resp.json() if resp.content else {"error": "ingester_error"}), resp.status_code
    return jsonify(resp.json())


@bp.route("/api/admin/feedback/<feedback_id>", methods=["PATCH"])
@require_auth
def update_admin_feedback(feedback_id: str):
    """Body : {status?, admin_comment?, ai_suggestion?}.

    processed_by est automatiquement renseigné depuis la session admin.
    """
    user = get_current_user()
    err = _ensure_admin_or_403(user)
    if err is not None:
        return err
    body = request.get_json(silent=True) or {}
    body["processed_by"] = (user or {}).get("sub") or "admin"
    try:
        resp = _call_ingester("PATCH", f"/api/v1/feedback/{feedback_id}", json_body=body)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    if resp.status_code >= 400:
        return jsonify(resp.json() if resp.content else {"error": "ingester_error"}), resp.status_code
    return jsonify(resp.json())


# ─── POST regenerate ─────────────────────────────────────────────────


@bp.route("/api/file/<file_id>/regenerate", methods=["POST"])
@require_auth
def regenerate_file(file_id: str):
    """Body : {scope: 'full'|'llm-only', reason: str}.

    Enregistre un feedback type=regenerate + déclenche le pipeline
    correspondant :

      • scope='llm-only' → appelle l'ingester ``/api/v1/audio/<id>/reprocess``
        existant (relance glossary → reformulation → meeting_analysis).
      • scope='full'     → enregistre seulement la demande (pas d'auto-
        run cette session — trop sensible). L'admin pourra déclencher
        manuellement la régénération full (re-Whisper + diarisation) via
        la vue admin une fois la file de feedback construite.

    Retour : ``{feedback_id, reprocessed, status}``.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    scope = (body.get("scope") or "").strip()
    reason = (body.get("reason") or "").strip()
    if scope not in ("full", "llm-only"):
        return jsonify({"error": "scope must be 'full' or 'llm-only'"}), 400
    if not reason:
        return jsonify({"error": "reason required"}), 400

    # 1. Enregistre la demande dans la table feedback.
    try:
        fb_resp = _call_ingester("POST", "/api/v1/feedback", json_body={
            "user_sub": user_sub,
            "file_id": file_id,
            "type": "regenerate",
            "payload": {"scope": scope, "reason": reason},
        })
        if fb_resp.status_code >= 400:
            return jsonify({"error": "feedback_store_failed"}), 502
        feedback = fb_resp.json()
    except req.RequestException:
        logger.exception("regenerate: feedback store unreachable")
        return jsonify({"error": "ingester_unavailable"}), 502

    # 2. Selon scope, déclenche le pipeline.
    if scope == "llm-only":
        # Délègue à l'endpoint existant (relance LLM aval). Le file_id
        # de l'URL est l'ID externe (uploaded_files.id) — on le mappe
        # vers l'audio.id interne (user_audio_files.id) avant l'appel
        # ingester, sinon 404 not_found.
        internal_id = _resolve_internal_audio_id(user_sub, file_id)
        if not internal_id:
            return jsonify({
                "feedback_id": feedback.get("id"),
                "reprocessed": False,
                "error": "audio_not_found_or_not_ready",
            }), 404
        try:
            # Timeout généreux : la chaîne LLM (glossary_correction → oob_cleaning
            # → reformulation → meeting_analysis → absentee_summary) tourne en
            # synchrone côté ingester et prend 30s–3min selon la longueur audio.
            # 10s par défaut = 502 quasi-systématique.
            r = _call_ingester("POST", f"/api/v1/audio/{internal_id}/reprocess",
                               json_body={"user_sub": user_sub, "force": True},
                               timeout=600)
        except req.RequestException:
            return jsonify({
                "feedback_id": feedback.get("id"),
                "reprocessed": False,
                "error": "ingester_unavailable",
            }), 502
        return jsonify({
            "feedback_id": feedback.get("id"),
            "reprocessed": (r.status_code < 400),
            "status": r.json() if r.content else {},
        }), (200 if r.status_code < 400 else r.status_code)

    # scope == 'full' → relance le pipeline complet (Whisper + pyannote +
    # chaîne LLM aval) via l'endpoint dédié de l'ingester. Mapping ID
    # externe → ID interne identique au cas llm-only.
    internal_id = _resolve_internal_audio_id(user_sub, file_id)
    if not internal_id:
        return jsonify({
            "feedback_id": feedback.get("id"),
            "reprocessed": False,
            "error": "audio_not_found_or_not_ready",
        }), 404
    try:
        # Réponse 202 immédiate côté ingester : pipeline en thread daemon.
        # Pas de long polling ici — le frontend rafraîchit transcript-status
        # toutes les ~3s pour suivre la progression (kevent_queued → ...
        # → kevent_completed) et garder le pulse (i) bleu actif.
        r = _call_ingester("POST", f"/api/v1/audio/{internal_id}/full-reprocess",
                           json_body={"user_sub": user_sub},
                           timeout=30)
    except req.RequestException:
        return jsonify({
            "feedback_id": feedback.get("id"),
            "reprocessed": False,
            "error": "ingester_unavailable",
        }), 502
    return jsonify({
        "feedback_id": feedback.get("id"),
        "reprocessed": (r.status_code < 400),
        "status": r.json() if r.content else {},
    }), (200 if r.status_code < 400 else r.status_code)


# ─── User glossary (édition manuelle) ─────────────────────────────


@bp.route("/api/my-glossary", methods=["GET"])
@require_auth
def list_my_glossary():
    """Liste paginée des termes du glossaire personnel de l'utilisateur."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    params = {
        "user_sub": user_sub,
        "limit": request.args.get("limit", 500),
        "offset": request.args.get("offset", 0),
    }
    try:
        resp = _call_ingester("GET", "/api/v1/user-glossary", params=params)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    if resp.status_code >= 400:
        return jsonify(resp.json() if resp.content else {"error": "ingester_error"}), resp.status_code
    return jsonify(resp.json())


@bp.route("/api/my-glossary", methods=["POST"])
@require_auth
def add_my_glossary_term():
    """Body : {term}. Ajoute un terme curaté pour l'utilisateur courant."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    term = (body.get("term") or "").strip()
    if not term:
        return jsonify({"error": "term required"}), 400
    try:
        resp = _call_ingester("POST", "/api/v1/user-glossary",
                              json_body={"user_sub": user_sub, "term": term})
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    return jsonify(resp.json() if resp.content else {}), resp.status_code


@bp.route("/api/my-glossary", methods=["PATCH"])
@require_auth
def patch_my_glossary_term():
    """Body : {term, curated_by_user?, blacklisted?}. Toggle d'un état."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    body["user_sub"] = user_sub
    try:
        resp = _call_ingester("PATCH", "/api/v1/user-glossary", json_body=body)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    return jsonify(resp.json() if resp.content else {}), resp.status_code


@bp.route("/api/files/resume-stuck-jobs", methods=["POST"])
@require_auth
def resume_stuck_jobs_user():
    """Body : ``{limit?: int}``. Déclenche un cycle du watchdog pipeline
    LIMITÉ aux fichiers de l'utilisateur connecté.

    Réutilise la route interne ``/api/v1/pipeline/resume-stuck-jobs`` en
    forçant ``user_sub`` au sub de l'utilisateur (jamais à la valeur
    fournie en body — sinon un user pourrait relancer les jobs d'un
    autre user via curl).
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    body["user_sub"] = user_sub  # force le scope
    try:
        resp = _call_ingester("POST", "/api/v1/pipeline/resume-stuck-jobs",
                              json_body=body)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    return jsonify(resp.json() if resp.content else {}), resp.status_code


@bp.route("/api/my-glossary/cleanup", methods=["POST"])
@require_auth
def cleanup_my_glossary():
    """Body : {dry_run?: bool, preview_limit?: int}.

    Délègue à l'ingester (cf. ``/api/v1/user-glossary/cleanup``) — purge
    rétroactive des termes peu spécifiques. Tracé séparément (avant
    delete-individual) pour que le frontend puisse l'invoquer sans
    confondre avec une suppression unitaire.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    body["user_sub"] = user_sub
    try:
        resp = _call_ingester("POST", "/api/v1/user-glossary/cleanup", json_body=body)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    return jsonify(resp.json() if resp.content else {}), resp.status_code


@bp.route("/api/my-glossary", methods=["DELETE"])
@require_auth
def delete_my_glossary_term():
    """Body : {term}. Supprime un terme du glossaire utilisateur."""
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    body["user_sub"] = user_sub
    try:
        resp = _call_ingester("DELETE", "/api/v1/user-glossary", json_body=body)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    return jsonify(resp.json() if resp.content else {}), resp.status_code


# ─── Correction inline d'un terme sur une transcription ──────────


@bp.route("/api/file/<file_id>/correct-term", methods=["POST"])
@require_auth
def correct_file_term(file_id: str):
    """Body : {old, new, add_to_glossary?, patch_text?, reprocess_llm?}.

    Délègue à l'ingester qui applique les 3 actions et trace l'audit
    dans user_feedback (type='correction').
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    body = request.get_json(silent=True) or {}
    body["user_sub"] = user_sub
    # Mappe le file_id externe (uploaded_files.id côté DMZ) vers
    # l'audio.id interne (user_audio_files.id côté internal-ingester) —
    # sinon l'ingester retourne 404 not_found.
    internal_id = _resolve_internal_audio_id(user_sub, file_id)
    if not internal_id:
        return jsonify({"error": "audio_not_found_or_not_ready"}), 404
    try:
        resp = _call_ingester("POST", f"/api/v1/audio/{internal_id}/correct-term",
                              json_body=body, timeout=30)
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    # Parse JSON défensivement : si l'ingester renvoie une 500 HTML (e.g.
    # unhandled exception → Flask debug page), .json() raise et le proxy
    # crashe en 500 silencieux. Fallback : retourner le raw text en error.
    try:
        body_out = resp.json() if resp.content else {}
    except ValueError:
        body_out = {"error": "ingester_invalid_response", "raw": (resp.text or '')[:500]}
    return jsonify(body_out), resp.status_code


@bp.route("/api/file/<file_id>/term-sources", methods=["GET"])
@require_auth
def file_term_sources(file_id: str):
    """Proxy vers ingester /api/v1/audio/<id>/term-sources?term=...

    Utilisé par le drawer "Sources brutes" du corrector CR (option C).
    Retourne les segments speaker-tagged qui contiennent le terme,
    avec timecodes pour permettre la ré-écoute audio et la propagation
    sélective vers la transcription brute/nettoyée.
    """
    user = get_current_user()
    user_sub = (user or {}).get("sub") or ""
    if not user_sub:
        return jsonify({"error": "unauthenticated"}), 401
    term = (request.args.get("term") or "").strip()
    query = (request.args.get("query") or "").strip()
    if not term and not query:
        return jsonify({"error": "term or query required"}), 400
    internal_id = _resolve_internal_audio_id(user_sub, file_id)
    if not internal_id:
        return jsonify({"error": "audio_not_found_or_not_ready"}), 404
    params = {"user_sub": user_sub}
    if term:
        params["term"] = term
    if query:
        params["query"] = query
    try:
        resp = _call_ingester(
            "GET", f"/api/v1/audio/{internal_id}/term-sources",
            params=params, timeout=10,
        )
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    return jsonify(resp.json() if resp.content else {}), resp.status_code


# ─── Export CSV admin ────────────────────────────────────────────────


@bp.route("/api/admin/feedback.csv", methods=["GET"])
@require_auth
def export_admin_feedback_csv():
    """Export CSV de tous les feedbacks (admin only)."""
    import csv
    from io import StringIO
    import json as _json
    from flask import Response

    err = _ensure_admin_or_403(get_current_user())
    if err is not None:
        return err
    try:
        resp = _call_ingester("GET", "/api/v1/feedback/all", params={"limit": 500, "offset": 0})
    except req.RequestException:
        return jsonify({"error": "ingester_unavailable"}), 502
    if resp.status_code >= 400:
        return jsonify({"error": "ingester_error"}), resp.status_code
    data = resp.json()
    items = data.get("items", [])

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "created_at", "user_sub", "file_id", "type",
        "thumb", "reasons", "free_text",
        "scope", "regenerate_reason",
        "status", "processed_at", "processed_by", "admin_comment", "ai_suggestion",
    ])
    for fb in items:
        p = fb.get("payload") or {}
        writer.writerow([
            fb.get("id"), fb.get("created_at"), fb.get("user_sub"), fb.get("file_id"),
            fb.get("type"),
            p.get("thumb") if fb.get("type") == "usefulness" else "",
            _json.dumps(p.get("reasons") or [], ensure_ascii=False) if fb.get("type") == "usefulness" else "",
            (p.get("free_text") or "")[:500] if fb.get("type") == "usefulness" else "",
            p.get("scope") if fb.get("type") == "regenerate" else "",
            (p.get("reason") or "")[:500] if fb.get("type") == "regenerate" else "",
            fb.get("status"), fb.get("processed_at"), fb.get("processed_by"),
            (fb.get("admin_comment") or "")[:500],
            (fb.get("ai_suggestion") or "")[:500],
        ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=feedbacks.csv"},
    )
