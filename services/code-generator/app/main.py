"""
Code Generator Service
======================
Authenticated interface (OIDC/Keycloak) for generating QR codes
and simple codes that link to the upload portal.

CHANGEMENT CLÉ : les tokens (simple_code + qr_token) sont générés
côté INTERNE par le token-issuer. Ce service ne fait que relayer
la demande et stocker une copie en base externe pour le suivi.
"""

import logging
import os
import sys
import json
import base64
import re
import subprocess
import tempfile
import secrets
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

import qrcode
import requests as req
from flask import Flask, redirect, url_for, session, render_template_string, jsonify, request, abort, send_file
from authlib.integrations.flask_client import OAuth

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    OIDCConfig, load_ext_db, CODE_TTL_MINUTES, CODE_TTL_MAX_MINUTES,
    MAX_UPLOADS_PER_SESSION, SECRET_KEY, UPLOAD_PORTAL_BASE_URL, load_s3_upload, load_s3_processed, load_s3_internal,
    UPLOAD_STATUS_VIEW_TTL_MINUTES, TOKEN_ISSUER_API_URL, INTERNAL_API_TOKEN,
    OIDC_OFFLINE_ACCESS, DEVICE_TOKEN_RETENTION_HOURS,
)
from libs.shared.app.oidc_refresh_store import store_refresh_token
from libs.shared.app.models import (
    ExternalBase, UploadSession, UploadedFile, SessionStatus, UploadStatus, UploadTokenOption
)
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.s3_helper import download_fileobj, delete_object, object_exists

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# ─── Flask App ──────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY

oidc_cfg = OIDCConfig()
db_cfg = load_ext_db()
s3_upload_cfg = load_s3_upload()
s3_processed_cfg = load_s3_processed()
s3_internal_cfg = load_s3_internal()
SessionLocal = None
ALLOW_SHORT_QR_TTL_SECONDS_TEST = os.getenv("ALLOW_SHORT_QR_TTL_SECONDS_TEST", "").lower() in {"1", "true", "yes"}
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").strip()
NORMALIZATION_ANALYSIS_MAX_SECONDS = max(30, int(os.getenv("NORMALIZATION_ANALYSIS_MAX_SECONDS", "180")))

# ─── OIDC Setup ─────────────────────────────────────────────

_OIDC_SCOPE_BASE = "openid email profile"
_OIDC_SCOPE = f"{_OIDC_SCOPE_BASE} offline_access" if OIDC_OFFLINE_ACCESS else _OIDC_SCOPE_BASE

oauth = OAuth(app)
oauth.register(
    name="keycloak",
    client_id=oidc_cfg.client_id,
    client_secret=oidc_cfg.client_secret,
    server_metadata_url=f"{oidc_cfg.issuer}/.well-known/openid-configuration",
    client_kwargs={"scope": _OIDC_SCOPE},
)
oidc_internal_issuer = os.getenv("OIDC_INTERNAL_ISSUER", oidc_cfg.issuer).rstrip("/")
if OIDC_OFFLINE_ACCESS:
    logger.info("OIDC offline_access scope ENABLED — refresh tokens will be persisted")
else:
    logger.info("OIDC offline_access scope DISABLED — set OIDC_OFFLINE_ACCESS=true to enable MCR push prerequisite")


# ─── Helpers ────────────────────────────────────────────────

def make_qr_image(url: str) -> BytesIO:
    """Generate a QR code image as PNG bytes."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def get_upload_portal_base_url() -> str:
    """
    Resolve upload portal URL for QR generation.
    Priority:
    1) Explicit PUBLIC_HOST env (recommended for server/public IP)
    2) Explicit non-localhost UPLOAD_PORTAL_BASE_URL
    3) Derive from request host / forwarded host and force port 8081
    4) Fallback to configured value
    """
    if PUBLIC_HOST:
        return f"{request.scheme}://{PUBLIC_HOST}:8081"

    configured = (UPLOAD_PORTAL_BASE_URL or "").strip().rstrip("/")
    lowered = configured.lower()
    if configured and ("localhost" not in lowered and "127.0.0.1" not in lowered):
        return configured

    forwarded_host = (request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    if forwarded_host:
        host = forwarded_host.split(":", 1)[0].strip("[]")
    else:
        host = request.host.split(":", 1)[0].strip("[]")
    if host and host not in {"localhost", "127.0.0.1"}:
        return f"{request.scheme}://{host}:8081"

    return configured or "http://localhost:8081"


def get_current_user():
    user = session.get("user")
    if not user:
        return None
    return user


def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def _decode_jwt_payload_unverified(token_value: str) -> dict:
    """Best-effort JWT payload decode (no signature verification)."""
    try:
        parts = token_value.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _oidc_request_with_retry(method: str, url: str, *, max_attempts: int = 3, retry_delay: float = 0.7, **kwargs):
    """Best-effort retry helper for intermittent OIDC network failures."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return req.request(method, url, **kwargs)
        except req.RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            logger.warning("OIDC request failed (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(retry_delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("OIDC request failed unexpectedly")


def request_token_from_internal(
    user: dict,
    ttl_minutes: int,
    max_uploads: int,
    ttl_seconds: int | None = None,
    auto_transcribe: bool = True,
) -> dict:
    """
    Appelle le token-issuer en zone INTERNE pour obtenir un (simple_code, qr_token).
    Le code-generator ne génère plus jamais de token lui-même.
    """
    payload = {
        "user_sub": user["sub"],
        "user_email": user.get("email"),
        "user_display_name": user.get("name"),
        "ttl_minutes": ttl_minutes,
        "max_uploads": max_uploads,
        "auto_transcribe": bool(auto_transcribe),
    }
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds

    resp = req.post(
        TOKEN_ISSUER_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def request_file_puller_api(path: str, *, json_body=None, timeout: int = 10) -> dict | None:
    """POST to file-puller's internal API. Returns the parsed JSON or None on 404.

    Used by the user-facing transcript download endpoints to fetch the
    user_audio_files row that lives in postgres-internal. Auth =
    INTERNAL_API_TOKEN (same bearer file-puller uses for /api/v1/pull).
    """
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL", "http://file-puller:8090").rstrip("/")
    resp = req.post(
        f"{base}{path}",
        json=json_body,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise req.HTTPError(f"file-puller {path} → {resp.status_code}: {resp.text[:200]}",
                            response=resp)
    return resp.json()


def request_internal_device_api(method: str, path: str, *, json_body=None, timeout: int = 10, params=None) -> dict:
    base = os.getenv("TOKEN_ISSUER_INTERNAL_BASE_URL", "http://token-issuer:8091").rstrip("/")
    resp = req.request(
        method,
        f"{base}{path}",
        json=json_body,
        params=params,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"error": resp.text[:200] or "internal_api_error"}
        raise req.HTTPError(str(err), response=resp)
    if not resp.text:
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def _get_owned_file(db, user_sub: str, file_id: str):
    """Return owned file row, ignoring trashed files and trashed sessions.

    Une fois `trashed_at` positionné, le fichier est invisible pour les
    endpoints download/stream/transcript — l'utilisateur ne peut plus
    l'utiliser, mais la row + S3 restent jusqu'à la purge 30j.
    """
    return (
        db.query(UploadedFile)
        .join(UploadSession, UploadSession.id == UploadedFile.session_id)
        .filter(
            UploadedFile.id == file_id,
            UploadSession.user_sub == user_sub,
            UploadedFile.trashed_at.is_(None),
            UploadSession.trashed_at.is_(None),
        )
        .first()
    )


# Durée de rétention de la corbeille avant purge définitive (DB + S3).
TRASH_RETENTION_DAYS = int(os.getenv("TRASH_RETENTION_DAYS", "30"))


def _purge_expired_trash(db, user_sub: str) -> tuple[int, int, int]:
    """Hard-delete des entries en corbeille depuis > TRASH_RETENTION_DAYS.

    Appelé en début de chaque GET /api/my-sessions pour assurer le
    "vidage automatique" promis dans l'UI sans dépendre d'un cron externe.
    Retourne (sessions_purged, files_purged, s3_objects_deleted).
    """
    threshold = datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)
    sessions_purged = 0
    files_purged = 0
    objects_deleted = 0

    # 1) Fichiers individuels en corbeille — leur session peut être active.
    trashed_files = (
        db.query(UploadedFile)
        .join(UploadSession, UploadSession.id == UploadedFile.session_id)
        .filter(
            UploadSession.user_sub == user_sub,
            UploadedFile.trashed_at.isnot(None),
            UploadedFile.trashed_at < threshold,
        )
        .all()
    )
    for f in trashed_files:
        for cfg, key in (
            (s3_upload_cfg, f.stored_filename),
            (s3_processed_cfg, f.transcoded_filename),
        ):
            if not key:
                continue
            try:
                delete_object(cfg, key)
                objects_deleted += 1
            except Exception:
                logger.debug("trash purge: S3 delete failed for %s", key, exc_info=True)
        try:
            t_cfg, t_key = _resolve_transferred_storage(db, f)
            if t_cfg and t_key:
                delete_object(t_cfg, t_key)
                objects_deleted += 1
        except Exception:
            logger.debug("trash purge: transferred S3 delete failed for %s", f.id, exc_info=True)
        db.delete(f)
        files_purged += 1

    # 2) Sessions en corbeille (cascade delete-orphan supprime aussi
    # les uploaded_files qui leur sont attachés — qu'elles soient
    # trashed ou non, la session porte la décision).
    trashed_sessions = (
        db.query(UploadSession)
        .filter(
            UploadSession.user_sub == user_sub,
            UploadSession.trashed_at.isnot(None),
            UploadSession.trashed_at < threshold,
        )
        .all()
    )
    for s in trashed_sessions:
        for f in s.uploads:
            for cfg, key in (
                (s3_upload_cfg, f.stored_filename),
                (s3_processed_cfg, f.transcoded_filename),
            ):
                if not key:
                    continue
                try:
                    delete_object(cfg, key)
                    objects_deleted += 1
                except Exception:
                    logger.debug("trash purge (session): S3 delete failed for %s", key, exc_info=True)
            files_purged += 1
        db.delete(s)
        sessions_purged += 1

    if sessions_purged or files_purged:
        db.commit()
        logger.info(
            "trash purge: user=%s sessions=%s files=%s s3_objects=%s",
            user_sub, sessions_purged, files_purged, objects_deleted,
        )
    return sessions_purged, files_purged, objects_deleted


def _resolve_file_storage(file_obj: UploadedFile):
    if file_obj.transcoded_filename:
        return s3_processed_cfg, file_obj.transcoded_filename
    return s3_upload_cfg, file_obj.stored_filename


def _resolve_source_storage(file_obj: UploadedFile):
    return s3_upload_cfg, file_obj.stored_filename


def _guess_audio_mime_from_key(key: str) -> str:
    ext = Path(key or "").suffix.lower()
    if ext in {".mp4", ".m4a"}:
        return "audio/mp4"
    if ext == ".wav":
        return "audio/wav"
    if ext == ".ogg":
        return "audio/ogg"
    if ext == ".opus":
        return "audio/ogg"
    if ext == ".mp3":
        return "audio/mpeg"
    return "application/octet-stream"


def _resolve_transcoded_storage(file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None, None
    return s3_processed_cfg, file_obj.transcoded_filename


def _compute_lifecycle_state(
    session: UploadSession,
    has_active_device: bool = False,
) -> str:
    """Compute the user-facing lifecycle state of a session at read time.

    Sémantique clarifiée :
      - La "grace QR" (``session.expires_at``, 5 min par défaut) ne sert que
        de fenêtre d'enrôlement. Une fois qu'un device a flashé le QR, c'est
        la rétention device (15j par défaut) qui pilote la fin de vie.
      - Donc tant qu'un ``device_enrollments`` du même qr_token est encore
        ``active`` (retention > now), la session est ``enrolled`` quoi qu'il
        arrive côté ``session.expires_at``.

    États :
      - ``pending_enrollment`` : grace QR encore valide, 0 device enrôlé
                                 → user en train de scanner
      - ``enrolled``           : ≥1 device actif (peut être post-grace, c'est OK)
      - ``expired_unused``     : grace passée, 0 device actif, 0 upload
                                 → safe to drop
      - ``expired_consumed``   : 0 device actif mais des fichiers existent
                                 → archive auditeur uniquement
    """
    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    qr_grace_past = bool(expires_at and expires_at < now)
    has_activity = (session.upload_count or 0) > 0

    # Source de vérité : le device. Si un device est encore actif, peu
    # importe l'état de la grace QR — l'utilisateur est en usage normal.
    if has_active_device:
        return "enrolled"
    # Plus de device actif : on retombe sur l'état "historique".
    if not qr_grace_past and not has_activity:
        return "pending_enrollment"
    if has_activity:
        return "expired_consumed"
    return "expired_unused"


def _resolve_transferred_storage(db, file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None, None
    session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
    if not session_obj:
        return None, None
    internal_key = f"{session_obj.user_sub}/{session_obj.simple_code}/{file_obj.transcoded_filename}"
    return s3_internal_cfg, internal_key


def _lookup_audio_outputs(db, file_obj: UploadedFile) -> dict | None:
    """Fetch the kevent / mcr / stub outputs for a file from file-puller.

    Returns the parsed JSON (transcription_text, speaker_tagged_text,
    glossary_corrected_text, meeting_analysis_json, etc.) or None if the
    user_audio_files row hasn't been created yet (file still in pipeline).

    Internal call — bearer-authenticated. Failure is logged but the caller
    surfaces a user-friendly error.
    """
    if not file_obj.transcoded_filename:
        return None
    session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
    if not session_obj:
        return None
    try:
        return request_file_puller_api(
            "/api/v1/audio/lookup",
            json_body={
                "user_sub": session_obj.user_sub,
                "simple_code": session_obj.simple_code,
                "stored_filename": file_obj.transcoded_filename,
            },
        )
    except req.RequestException:
        logger.exception("file-puller lookup failed for file_id=%s", file_obj.id)
        return None


def _run_loudnorm_measure(input_path: str, target_i: float = -16.0, target_tp: float = -1.5, target_lra: float = 11.0):
    """
    Run a loudnorm analysis pass and return measured values.
    Returns keys: i, tp, lra.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", input_path,
        "-t", str(NORMALIZATION_ANALYSIS_MAX_SECONDS),
        "-af", f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg loudnorm analysis failed")

    matches = re.findall(r"\{[\s\S]*?\}", proc.stderr or "")
    if not matches:
        raise RuntimeError("loudnorm output not found")
    data = json.loads(matches[-1])

    return {
        "i": float(data.get("input_i")),
        "tp": float(data.get("input_tp")),
        "lra": float(data.get("input_lra")),
    }


# ─── Routes ─────────────────────────────────────────────────

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "code-generator"}), 200


@app.route("/")
@require_auth
def index():
    user = get_current_user()
    return render_template_string(
        INDEX_TEMPLATE,
        user=user,
        short_ttl_enabled=ALLOW_SHORT_QR_TTL_SECONDS_TEST,
        device_retention_days=max(1, DEVICE_TOKEN_RETENTION_HOURS // 24),
    )


@app.route("/login")
def login():
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    session["oidc_state"] = state
    session["oidc_nonce"] = nonce
    params = {
        "response_type": "code",
        "client_id": oidc_cfg.client_id,
        "redirect_uri": oidc_cfg.redirect_uri,
        "scope": _OIDC_SCOPE,
        "state": state,
        "nonce": nonce,
    }
    auth_url = f"{oidc_cfg.issuer.rstrip('/')}/protocol/openid-connect/auth?{urlencode(params)}"
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    # Callback can be hit twice by browser retry/prefetch. If already authenticated,
    # skip token exchange to avoid reusing the one-time authorization code.
    if session.get("user"):
        session.pop("oidc_state", None)
        session.pop("oidc_nonce", None)
        return redirect(url_for("index"))

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not code or not state or state != session.get("oidc_state"):
        return "OIDC callback invalide (state/code).", 400

    try:
        token_resp = _oidc_request_with_retry(
            "POST",
            f"{oidc_internal_issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": oidc_cfg.redirect_uri,
                "client_id": oidc_cfg.client_id,
                "client_secret": oidc_cfg.client_secret,
            },
            timeout=10,
        )
    except req.RequestException:
        logger.exception("OIDC token endpoint unreachable")
        return "OIDC indisponible (token endpoint). Réessaie.", 502

    if token_resp.status_code >= 400:
        body = (token_resp.text or "")[:500]
        logger.warning(
            "OIDC token exchange failed: status=%s body=%s",
            token_resp.status_code,
            body,
        )
        # Keycloak returns invalid_grant when an auth code is already consumed.
        # Restarting login avoids blocking user on a stale callback URL.
        if "invalid_grant" in body or "Code not valid" in body:
            session.pop("oidc_state", None)
            session.pop("oidc_nonce", None)
            return redirect(url_for("login"))
        return "Echec de connexion OIDC (code expiré ou déjà utilisé).", 400

    try:
        token = token_resp.json()
    except Exception:
        logger.warning("OIDC token response is not JSON: %s", (token_resp.text or "")[:300])
        return "Réponse OIDC invalide (token).", 502

    try:
        userinfo_resp = _oidc_request_with_retry(
            "GET",
            f"{oidc_internal_issuer}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {token.get('access_token', '')}"},
            timeout=10,
        )
        if userinfo_resp.status_code >= 400:
            logger.warning(
                "OIDC userinfo failed: status=%s body=%s",
                userinfo_resp.status_code,
                (userinfo_resp.text or "")[:500],
            )
            userinfo = _decode_jwt_payload_unverified(token.get("id_token", ""))
            if not userinfo:
                return "Echec de récupération du profil OIDC.", 400
            logger.info("OIDC userinfo fallback to id_token claims")
        else:
            userinfo = userinfo_resp.json()
    except Exception:
        logger.exception("Failed to fetch userinfo from Keycloak")
        userinfo = _decode_jwt_payload_unverified(token.get("id_token", ""))
        if not userinfo:
            return "Erreur OIDC (userinfo). Réessaie.", 502
        logger.info("OIDC userinfo exception fallback to id_token claims")

    session["user"] = {
        "sub": userinfo.get("sub", ""),
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", userinfo.get("preferred_username", "")),
    }
    session["id_token"] = token.get("id_token", "")
    session.pop("oidc_state", None)
    session.pop("oidc_nonce", None)

    # Persist the refresh token (encrypted, server-side) for the asynchronous
    # MCR push later. Best-effort: failure here MUST NOT break the login flow.
    if OIDC_OFFLINE_ACCESS:
        try:
            store_refresh_token(
                user_sub=userinfo.get("sub", ""),
                refresh_token=token.get("refresh_token"),
                keycloak_iss=oidc_cfg.issuer,
                user_email=userinfo.get("email", ""),
            )
        except Exception:
            logger.exception("Failed to persist OIDC refresh token (login still succeeded)")

    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    id_token_hint = session.get("id_token")
    session.clear()

    # RP-initiated logout on OIDC provider to avoid immediate SSO relogin.
    post_logout_redirect_uri = oidc_cfg.redirect_uri.replace("/auth/callback", "/")
    params = {
        "post_logout_redirect_uri": post_logout_redirect_uri,
        "client_id": oidc_cfg.client_id,
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    logout_url = f"{oidc_cfg.issuer.rstrip('/')}/protocol/openid-connect/logout?{urlencode(params)}"
    return redirect(logout_url)


@app.route("/api/generate-code", methods=["POST"])
@require_auth
def api_generate_code():
    """
    Demande un token au token-issuer (zone interne), puis stocke
    une copie locale en base externe pour le suivi des uploads.
    """
    user = get_current_user()
    data = request.get_json(silent=True) or {}

    ttl_raw = str(data.get("ttl_minutes", CODE_TTL_MINUTES))
    ttl_seconds = None
    if ttl_raw.endswith("s"):
        if not ALLOW_SHORT_QR_TTL_SECONDS_TEST:
            return jsonify({"error": "Short TTL test mode is disabled"}), 400
        try:
            ttl_seconds = int(ttl_raw[:-1])
        except ValueError:
            return jsonify({"error": "Invalid short TTL value"}), 400
        if ttl_seconds not in {15, 30}:
            return jsonify({"error": "Allowed short TTL values: 15s, 30s"}), 400
        ttl_minutes = 1
    else:
        ttl_minutes = min(
            max(int(ttl_raw), 1),
            CODE_TTL_MAX_MINUTES,
        )
    max_uploads = min(
        max(int(data.get("max_uploads", MAX_UPLOADS_PER_SESSION)), 1),
        50,
    )
    auto_transcribe = bool(data.get("auto_transcribe", True))

    # ── Appel au token-issuer INTERNE ──
    try:
        token_data = request_token_from_internal(
            user,
            ttl_minutes,
            max_uploads,
            ttl_seconds=ttl_seconds,
            auto_transcribe=auto_transcribe,
        )
    except req.RequestException as e:
        logger.exception("Failed to request token from internal zone")
        return jsonify({"error": "Service de génération de token indisponible. Réessayez."}), 503

    simple_code = token_data["simple_code"]
    qr_token = token_data["qr_token"]
    expires_at = datetime.fromisoformat(token_data["expires_at"])

    # ── Stocker copie en base externe (pour suivi uploads) ──
    upload_session = UploadSession(
        id=uuid4(),
        user_sub=user["sub"],
        user_email=user.get("email"),
        user_display_name=user.get("name"),
        simple_code=simple_code,
        qr_token=qr_token,
        max_uploads=max_uploads,
        ttl_minutes=ttl_minutes,
        expires_at=expires_at,
        status_view_expires_at=expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES),
    )

    db = SessionLocal()
    try:
        db.add(upload_session)
        db.add(
            UploadTokenOption(
                id=uuid4(),
                qr_token=qr_token,
                simple_code=simple_code,
                auto_transcribe=auto_transcribe,
            )
        )
        db.commit()
    finally:
        db.close()

    upload_url = f"{get_upload_portal_base_url()}/upload/{qr_token}"

    return jsonify({
        "session_id": str(upload_session.id),
        "simple_code": simple_code,
        "qr_token": qr_token,
        "upload_url": upload_url,
        "expires_at": expires_at.isoformat(),
        "ttl_minutes": ttl_minutes,
        "ttl_seconds": token_data.get("ttl_seconds"),
        "max_uploads": max_uploads,
        "auto_transcribe": auto_transcribe,
    })


@app.route("/api/qr-image/<qr_token>")
@require_auth
def api_qr_image(qr_token):
    upload_url = f"{get_upload_portal_base_url()}/upload/{qr_token}"
    buf = make_qr_image(upload_url)
    return buf.getvalue(), 200, {"Content-Type": "image/png"}


@app.route("/api/my-sessions")
@require_auth
def api_my_sessions():
    user = get_current_user()
    db = SessionLocal()
    try:
        # Avant de servir la liste : purge définitive opportuniste des items
        # restés en corbeille > 30 jours. Idempotent ; coût borné (filtre
        # indexé sur trashed_at + scope user_sub).
        try:
            _purge_expired_trash(db, user["sub"])
        except Exception:
            db.rollback()
            logger.debug("trash purge skipped (non-fatal)", exc_info=True)

        sessions = db.query(UploadSession).filter(
            UploadSession.user_sub == user["sub"],
            UploadSession.trashed_at.is_(None),
        ).order_by(UploadSession.created_at.desc()).limit(20).all()

        # On récupère la liste des devices encore actifs pour cet utilisateur
        # (token-issuer GET /api/v1/devices). Permet à _compute_lifecycle_state
        # de décider "enrolled" même quand session.expires_at est dans le passé,
        # tant qu'un device est encore valide en rétention.
        active_qr_tokens: set[str] = set()
        try:
            devices = request_internal_device_api(
                "GET",
                "/api/v1/devices",
                params={"user_sub": user.get("sub", "")},
            )
            now = datetime.now(timezone.utc)
            for d in (devices if isinstance(devices, list) else []):
                if not isinstance(d, dict):
                    continue
                if (d.get("status") or "").lower() != "active":
                    continue
                retention_raw = d.get("retention_expires_at")
                if retention_raw:
                    try:
                        retention = datetime.fromisoformat(retention_raw.replace("Z", "+00:00"))
                        if retention.tzinfo is None:
                            retention = retention.replace(tzinfo=timezone.utc)
                        if retention <= now:
                            continue
                    except Exception:
                        pass
                qr = (d.get("qr_token") or "").strip()
                if qr:
                    active_qr_tokens.add(qr)
        except Exception:
            # Best-effort : si token-issuer ne répond pas, on retombe sur le
            # calcul historique (basé uniquement sur session.expires_at).
            logger.debug("Could not fetch devices for lifecycle enrichment", exc_info=True)

        reconciled = 0
        result = []
        for s in sessions:
            uploads = []
            for f in s.uploads:
                # Skip soft-deleted files (en corbeille) — invisibles tant
                # que pas purgés définitivement (>30j) par _purge_expired_trash.
                if f.trashed_at is not None:
                    continue
                # Self-heal: if transfer callback was missed but object exists internally,
                # promote the status to TRANSFERRED so the UI can resume correctly.
                if f.status in {UploadStatus.READY_FOR_TRANSFER, UploadStatus.TRANSFERRING} and f.transcoded_filename:
                    try:
                        t_cfg, t_key = _resolve_transferred_storage(db, f)
                        if t_cfg and t_key and object_exists(t_cfg, t_key):
                            f.status = UploadStatus.TRANSFERRED
                            f.status_message = "Fichier intégré à votre compte. Transcription en cours... (rattrapage auto)"
                            if not f.transferred_at:
                                f.transferred_at = datetime.now(timezone.utc)
                            reconciled += 1
                    except Exception:
                        logger.debug("Unable to reconcile transfer status for %s", f.id, exc_info=True)

                source_available = False
                if f.stored_filename:
                    try:
                        source_available = object_exists(s3_upload_cfg, f.stored_filename)
                    except Exception:
                        logger.debug("Unable to verify source object presence for %s", f.id, exc_info=True)

                transcoded_available = False
                if f.transcoded_filename:
                    try:
                        transcoded_available = object_exists(s3_processed_cfg, f.transcoded_filename)
                    except Exception:
                        logger.debug("Unable to verify transcoded object presence for %s", f.id, exc_info=True)

                transferred_available = False
                if f.status == UploadStatus.TRANSFERRED and f.transcoded_filename:
                    try:
                        t_cfg, t_key = _resolve_transferred_storage(db, f)
                        transferred_available = bool(t_cfg and t_key and object_exists(t_cfg, t_key))
                    except Exception:
                        logger.debug("Unable to verify transferred object presence for %s", f.id, exc_info=True)

                uploads.append({
                    "id": str(f.id),
                    "original_filename": f.original_filename,
                    "status": f.status.value,
                    "status_message": f.status_message,
                    "audio_quality_score": f.audio_quality_score,
                    "audio_duration_seconds": f.audio_duration_seconds,
                    "created_at": f.created_at.isoformat(),
                    "updated_at": f.updated_at.isoformat() if f.updated_at else None,
                    "download_url": f"/api/file/download/{f.id}",
                    "stream_url": f"/api/file/stream/{f.id}",
                    "source_available": source_available,
                    "source_download_url": f"/api/file/download-source/{f.id}" if source_available else None,
                    "source_stream_url": f"/api/file/stream-source/{f.id}" if source_available else None,
                    "transcoded_available": transcoded_available,
                    "transcoded_download_url": f"/api/file/download-transcoded/{f.id}" if transcoded_available else None,
                    "transcoded_stream_url": f"/api/file/stream-transcoded/{f.id}" if transcoded_available else None,
                    "transferred_available": transferred_available,
                    "transferred_download_url": f"/api/file/download-transferred/{f.id}" if transferred_available else None,
                    "transferred_stream_url": f"/api/file/stream-transferred/{f.id}" if transferred_available else None,
                    "impact_url": f"/api/file/normalization-impact/{f.id}",
                })
            result.append({
                "id": str(s.id),
                "simple_code": s.simple_code,
                "qr_token": s.qr_token,
                "status": s.status.value,
                "upload_count": s.upload_count,
                "max_uploads": s.max_uploads,
                "expires_at": s.expires_at.isoformat(),
                "created_at": s.created_at.isoformat(),
                "lifecycle_state": _compute_lifecycle_state(
                    s,
                    has_active_device=(s.qr_token or "") in active_qr_tokens,
                ),
                "uploads": uploads,
            })
        if reconciled:
            db.commit()
            logger.info("Auto-reconciled %s transfer status entries for user %s", reconciled, user.get("sub"))
        return jsonify(result)
    finally:
        db.close()


@app.route("/api/my-devices")
@require_auth
def api_my_devices():
    user = get_current_user()
    db = SessionLocal()
    try:
        devices = request_internal_device_api(
            "GET",
            "/api/v1/devices",
            params={"user_sub": user.get("sub", "")},
        )
        devices = devices if isinstance(devices, list) else []

        # Enrich device list with recent upload counters from external DB sessions.
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)
        sessions = (
            db.query(UploadSession)
            .filter(UploadSession.user_sub == user.get("sub", ""))
            .all()
        )
        session_by_qr = {}
        for s in sessions:
            token = (s.qr_token or "").strip()
            if not token:
                continue
            prev = session_by_qr.get(token)
            if prev is None or (s.created_at and prev.created_at and s.created_at > prev.created_at):
                session_by_qr[token] = s

        enriched = []
        for d in devices:
            item = dict(d) if isinstance(d, dict) else {}
            qr_token = (item.get("qr_token") or "").strip()
            s = session_by_qr.get(qr_token)
            if s is not None:
                recent_uploads_24h = 0
                for f in (s.uploads or []):
                    created = f.created_at
                    if created is None:
                        continue
                    created_utc = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                    if created_utc >= day_ago:
                        recent_uploads_24h += 1
                remaining_uploads = max(0, int(s.max_uploads or 0) - int(s.upload_count or 0))
                session_max_uploads = int(s.max_uploads or 0)
                session_upload_count = int(s.upload_count or 0)
                expires_at = s.expires_at
                expiring_soon = False
                if expires_at:
                    exp_utc = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
                    # Saillance plus tôt (7j) que la valeur initiale 2j — l'utilisateur
                    # a le temps de prolonger sans urgence.
                    expiring_soon = exp_utc <= (now + timedelta(days=7))
                item["recent_uploads_24h"] = recent_uploads_24h
                item["remaining_uploads"] = remaining_uploads
                item["session_max_uploads"] = session_max_uploads
                item["session_upload_count"] = session_upload_count
                item["session_simple_code"] = s.simple_code
                item["session_expiring_soon"] = expiring_soon
                item["session_expires_at"] = expires_at.isoformat() if expires_at else None
            else:
                item["recent_uploads_24h"] = 0
                item["remaining_uploads"] = 0
                item["session_max_uploads"] = 0
                item["session_upload_count"] = 0
                item["session_simple_code"] = None
                item["session_expiring_soon"] = False
                item["session_expires_at"] = None
            enriched.append(item)

        return jsonify(enriched)
    except Exception:
        logger.exception("Failed to list enrolled devices for user %s", user.get("sub"))
        return jsonify({"error": "device_list_unavailable"}), 503
    finally:
        db.close()


@app.route("/api/my-devices/<device_id>/rename", methods=["POST"])
@require_auth
def api_rename_device(device_id):
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    name = (data.get("device_name") or "").strip()
    if not name:
        return jsonify({"error": "device_name requis"}), 400
    try:
        request_internal_device_api(
            "POST",
            f"/api/v1/devices/{device_id}/rename",
            json_body={"user_sub": user.get("sub", ""), "device_name": name},
        )
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to rename device %s for user %s", device_id, user.get("sub"))
        return jsonify({"error": "device_rename_failed"}), 500


@app.route("/api/my-devices/<device_id>/revoke", methods=["POST"])
@require_auth
def api_revoke_device(device_id):
    user = get_current_user()
    try:
        request_internal_device_api(
            "POST",
            f"/api/v1/devices/{device_id}/revoke",
            json_body={"user_sub": user.get("sub", ""), "reason": "revoked_from_qr_ui"},
        )
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to revoke device %s for user %s", device_id, user.get("sub"))
        return jsonify({"error": "device_revoke_failed"}), 500


@app.route("/api/my-devices/<device_id>", methods=["DELETE"])
@require_auth
def api_delete_device(device_id):
    """Permanently delete a device enrollment (no audit row left in DB).

    Stronger than revoke — used by the user to clean up old / duplicate
    enrollments. The UI requires a double-confirm before calling this.
    """
    user = get_current_user()
    try:
        request_internal_device_api(
            "DELETE",
            f"/api/v1/devices/{device_id}",
            json_body={"user_sub": user.get("sub", "")},
        )
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to delete device %s for user %s", device_id, user.get("sub"))
        return jsonify({"error": "device_delete_failed"}), 500


@app.route("/api/my-devices/revoke-all", methods=["POST"])
@require_auth
def api_revoke_all_devices():
    user = get_current_user()
    try:
        data = request_internal_device_api(
            "POST",
            "/api/v1/devices/revoke-all",
            json_body={"user_sub": user.get("sub", ""), "reason": "revoked_all_from_qr_ui"},
        )
        return jsonify({"ok": True, "revoked": int(data.get("revoked", 0))})
    except Exception:
        logger.exception("Failed to revoke all devices for user %s", user.get("sub"))
        return jsonify({"error": "device_revoke_all_failed"}), 500


@app.route("/api/my-token/renew-7d", methods=["POST"])
@require_auth
def api_renew_token_7d():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    qr_token = (payload.get("qr_token") or "").strip()
    if not qr_token:
        return jsonify({"error": "qr_token_required"}), 400
    ttl_raw = str(payload.get("ttl_minutes", "")).strip()
    ttl_minutes = None
    ttl_seconds = None
    if ttl_raw:
        if ttl_raw.endswith("s"):
            if not ALLOW_SHORT_QR_TTL_SECONDS_TEST:
                return jsonify({"error": "Short TTL test mode is disabled"}), 400
            try:
                ttl_seconds = int(ttl_raw[:-1])
            except ValueError:
                return jsonify({"error": "Invalid ttl format"}), 400
            if ttl_seconds not in {15, 30}:
                return jsonify({"error": "Allowed ttl_seconds values are 15 or 30"}), 400
            ttl_minutes = 1
        else:
            try:
                ttl_minutes = min(max(int(ttl_raw), 1), CODE_TTL_MAX_MINUTES)
            except ValueError:
                return jsonify({"error": "Invalid ttl format"}), 400
    add_uploads = min(max(int(payload.get("add_uploads", 0) or 0), 0), 500)

    db = SessionLocal()
    try:
        data = request_internal_device_api(
            "POST",
            "/api/v1/tokens/extend-7d",
            json_body={
                "user_sub": user["sub"],
                "qr_token": qr_token,
                "ttl_minutes": ttl_minutes,
                "ttl_seconds": ttl_seconds,
                "add_uploads": add_uploads,
            },
        )
        new_expires_raw = data.get("expires_at")
        if not new_expires_raw:
            raise ValueError("missing expires_at from internal API")

        new_expires_at = datetime.fromisoformat(str(new_expires_raw))
        if new_expires_at.tzinfo is None:
            new_expires_at = new_expires_at.replace(tzinfo=timezone.utc)
        new_status_view_raw = data.get("status_view_expires_at")
        if new_status_view_raw:
            new_status_view = datetime.fromisoformat(str(new_status_view_raw))
            if new_status_view.tzinfo is None:
                new_status_view = new_status_view.replace(tzinfo=timezone.utc)
        else:
            new_status_view = new_expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES)

        session_obj = (
            db.query(UploadSession)
            .filter(UploadSession.user_sub == user["sub"], UploadSession.qr_token == qr_token)
            .first()
        )
        if session_obj:
            session_obj.expires_at = new_expires_at
            session_obj.status_view_expires_at = new_status_view
            session_obj.status = SessionStatus.ACTIVE
            if add_uploads > 0:
                session_obj.max_uploads = min(1000, int(session_obj.max_uploads or 0) + add_uploads)
            db.commit()
        else:
            db.rollback()

        return jsonify(
            {
                "ok": True,
                "expires_at": new_expires_at.isoformat(),
                "renew_days": int(data.get("renew_days", 7)),
                "max_uploads": int(data.get("max_uploads", 0) or 0),
            }
        )
    except req.HTTPError as err:
        db.rollback()
        if err.response is not None:
            try:
                body = err.response.json()
            except Exception:
                body = {"error": "internal_api_error"}
            return jsonify({"error": body.get("error", "renew_failed")}), err.response.status_code
        return jsonify({"error": "renew_failed"}), 502
    except Exception:
        db.rollback()
        logger.exception("Failed to renew token for user %s", user.get("sub"))
        return jsonify({"error": "renew_failed"}), 500
    finally:
        db.close()


@app.route("/api/my-sessions/<session_id>/renew-7d", methods=["POST"])
@require_auth
def api_renew_session_7d(session_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        session_obj = (
            db.query(UploadSession)
            .filter(UploadSession.id == session_id, UploadSession.user_sub == user["sub"])
            .first()
        )
        if not session_obj:
            return jsonify({"error": "session_not_found"}), 404

        payload = request.get_json(silent=True) or {}
        ttl_raw = str(payload.get("ttl_minutes", "")).strip()
        ttl_minutes = None
        ttl_seconds = None
        if ttl_raw:
            if ttl_raw.endswith("s"):
                if not ALLOW_SHORT_QR_TTL_SECONDS_TEST:
                    return jsonify({"error": "Short TTL test mode is disabled"}), 400
                try:
                    ttl_seconds = int(ttl_raw[:-1])
                except ValueError:
                    return jsonify({"error": "Invalid ttl format"}), 400
                if ttl_seconds not in {15, 30}:
                    return jsonify({"error": "Allowed ttl_seconds values are 15 or 30"}), 400
                ttl_minutes = 1
            else:
                try:
                    ttl_minutes = min(max(int(ttl_raw), 1), CODE_TTL_MAX_MINUTES)
                except ValueError:
                    return jsonify({"error": "Invalid ttl format"}), 400
        add_uploads = min(max(int(payload.get("add_uploads", 0) or 0), 0), 500)

        data = request_internal_device_api(
            "POST",
            "/api/v1/tokens/extend-7d",
            json_body={
                "user_sub": user["sub"],
                "qr_token": session_obj.qr_token,
                "ttl_minutes": ttl_minutes,
                "ttl_seconds": ttl_seconds,
                "add_uploads": add_uploads,
            },
        )
        new_expires_raw = data.get("expires_at")
        if not new_expires_raw:
            raise ValueError("missing expires_at from internal API")

        new_expires_at = datetime.fromisoformat(str(new_expires_raw))
        if new_expires_at.tzinfo is None:
            new_expires_at = new_expires_at.replace(tzinfo=timezone.utc)
        new_status_view_raw = data.get("status_view_expires_at")
        if new_status_view_raw:
            new_status_view = datetime.fromisoformat(str(new_status_view_raw))
            if new_status_view.tzinfo is None:
                new_status_view = new_status_view.replace(tzinfo=timezone.utc)
        else:
            new_status_view = new_expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES)

        session_obj.expires_at = new_expires_at
        session_obj.status_view_expires_at = new_status_view
        session_obj.status = SessionStatus.ACTIVE
        if add_uploads > 0:
            session_obj.max_uploads = min(1000, int(session_obj.max_uploads or 0) + add_uploads)
        db.commit()

        return jsonify(
            {
                "ok": True,
                "session_id": str(session_obj.id),
                "expires_at": session_obj.expires_at.isoformat(),
                "renew_days": int(data.get("renew_days", 7)),
                "max_uploads": int(data.get("max_uploads", 0) or 0),
            }
        )
    except req.HTTPError as err:
        db.rollback()
        if err.response is not None:
            try:
                body = err.response.json()
            except Exception:
                body = {"error": "internal_api_error"}
            return jsonify({"error": body.get("error", "renew_failed")}), err.response.status_code
        return jsonify({"error": "renew_failed"}), 502
    except Exception:
        db.rollback()
        logger.exception("Failed to renew session %s for user %s", session_id, user.get("sub"))
        return jsonify({"error": "renew_failed"}), 500
    finally:
        db.close()


@app.route("/api/device/enroll-proxy", methods=["POST"])
def api_device_enroll_proxy():
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        data = request_internal_device_api("POST", "/api/v1/enroll-device", json_body=payload)
        return jsonify(data)
    except req.HTTPError as err:
        # Preserve upstream's error code + human message (e.g. session_already_bound).
        if err.response is not None:
            try:
                return jsonify(err.response.json()), err.response.status_code
            except Exception:
                return jsonify({"error": "device_enroll_proxy_failed"}), err.response.status_code
        return jsonify({"error": "device_enroll_proxy_failed"}), 502
    except Exception:
        logger.exception("Device enroll proxy failed")
        return jsonify({"error": "device_enroll_proxy_failed"}), 502


@app.route("/api/device/validate-proxy", methods=["POST"])
def api_device_validate_proxy():
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        data = request_internal_device_api("POST", "/api/v1/validate-device", json_body=payload)
        return jsonify(data)
    except req.HTTPError as err:
        if err.response is not None:
            try:
                return jsonify(err.response.json()), err.response.status_code
            except Exception:
                return jsonify({"valid": False, "reason": "upstream_error"}), 502
        return jsonify({"valid": False, "reason": "upstream_error"}), 502
    except Exception:
        logger.exception("Device validate proxy failed")
        return jsonify({"valid": False, "reason": "proxy_error"}), 502


@app.route("/api/file/download/<file_id>")
@require_auth
def api_file_download(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_file_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(
            data,
            mimetype=file_obj.mime_type or "application/octet-stream",
            as_attachment=True,
            download_name=file_obj.original_filename,
        )
    finally:
        db.close()


@app.route("/api/file/stream/<file_id>")
@require_auth
def api_file_stream(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_file_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(
            data,
            mimetype=file_obj.mime_type or "audio/wav",
            as_attachment=False,
            download_name=file_obj.original_filename,
        )
    finally:
        db.close()


@app.route("/api/file/download-source/<file_id>")
@require_auth
def api_file_download_source(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_source_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(
            data,
            mimetype=file_obj.mime_type or "application/octet-stream",
            as_attachment=True,
            download_name=file_obj.original_filename,
        )
    finally:
        db.close()


@app.route("/api/file/stream-source/<file_id>")
@require_auth
def api_file_stream_source(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_source_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(
            data,
            mimetype=file_obj.mime_type or "audio/*",
            as_attachment=False,
            download_name=file_obj.original_filename,
        )
    finally:
        db.close()


@app.route("/api/file/download-transcoded/<file_id>")
@require_auth
def api_file_download_transcoded(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_transcoded_storage(file_obj)
        if not cfg or not key:
            abort(404, "Transcoded file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(
            data,
            mimetype=_guess_audio_mime_from_key(key),
            as_attachment=True,
            download_name=f"{Path(file_obj.original_filename).stem}_transcoded{suffix}",
        )
    finally:
        db.close()


@app.route("/api/file/stream-transcoded/<file_id>")
@require_auth
def api_file_stream_transcoded(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_transcoded_storage(file_obj)
        if not cfg or not key:
            abort(404, "Transcoded file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(
            data,
            mimetype=_guess_audio_mime_from_key(key),
            as_attachment=False,
            download_name=f"{Path(file_obj.original_filename).stem}_transcoded{suffix}",
        )
    finally:
        db.close()


def _send_text_attachment(text: str, filename: str, mime: str = "text/plain"):
    """Tiny helper to wrap text into a downloadable file response."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, indent=2)
    return send_file(
        BytesIO(text.encode("utf-8")),
        mimetype=mime,
        as_attachment=True,
        download_name=filename,
    )


def _maybe_prepend_key_points(body: str, key_points_md: str | None, fmt: str) -> str:
    """Prefix the body with a 'Points clés' section for rendered formats.

    Only applied to md/docx/odt — plain .txt stays untouched. The CR
    (meeting analysis) endpoint skips this since its sections already cover
    decisions/recommendations.
    """
    if not key_points_md or fmt not in ("md", "docx", "odt"):
        return body
    return f"## Points clés\n\n{key_points_md}\n\n---\n\n{body}"


def _build_download_basename(file_obj: UploadedFile, audio: dict | None, slot: str) -> str:
    """Build the user-visible filename for a transcript/CR download.

    Priority for the human-readable stem:
      1. ``audio["suggested_filename"]`` produced by the LLM-based renamer
         (kevent pipeline) — falls back to original_filename otherwise.
      2. Date suffix (short YYYY-MM-DD) derived from ``transcription_completed_at``
         (same source as the LLM rename), else the file's created_at.

    The slot describes which output is being served (e.g. ``transcript``,
    ``transcript-tagged``, ``transcript-corrected``, ``meeting-cr``) — used
    only as a contextual suffix when no LLM-suggested filename is available.
    """
    stem = ""
    if audio and audio.get("suggested_filename"):
        stem = audio["suggested_filename"].strip()
    if not stem:
        stem = Path(file_obj.original_filename or "audio").stem
        if slot:
            stem = f"{stem}_{slot}"
    # Date suffix
    date_src = None
    if audio and audio.get("transcription_completed_at"):
        try:
            date_src = audio["transcription_completed_at"][:10]  # YYYY-MM-DD
        except Exception:
            date_src = None
    if not date_src and file_obj.created_at:
        date_src = file_obj.created_at.strftime("%Y-%m-%d")
    if date_src and date_src not in stem:
        stem = f"{stem} {date_src}"
    return stem


def _audio_or_404(db, user_sub: str, file_id: str):
    """Resolve an UploadedFile owned by the user and its audio outputs.

    Returns ``(file_obj, audio_dict_or_none)`` or aborts 404 if the file
    isn't owned by the caller. ``audio_dict_or_none`` is None when the
    user_audio_files row hasn't been created yet (transcription pending).
    """
    file_obj = _get_owned_file(db, user_sub, file_id)
    if not file_obj:
        abort(404, "File not found")
    return file_obj, _lookup_audio_outputs(db, file_obj)


# ─── Transcription / CR downloads (Feature 3) ───────────────────────────────
# Each route serves one output format. file-puller is queried once per call —
# acceptable since the user only clicks one download at a time.
# TODO follow-up: route `/api/file/transcript-to-drive/<file_id>` to drop the
# generated document into the user's personal Drive folder (Google Drive or
# any provider configured per-tenant). Out of scope for this PR — needs OAuth
# scope + per-user Drive credentials.

_TRANSCRIPT_KIND_TO_COLUMN = {
    "transcript": "transcription_text",
    "transcript-tagged": "speaker_tagged_text",
    "transcript-corrected": "glossary_corrected_text",
    "transcript-cleaned": "cleaned_text",
    "transcript-reformulated": "reformulated_text",
}


@app.route("/api/file/transcript/<kind>/<ext>/<file_id>")
@require_auth
def api_file_transcript_download(kind, ext, file_id):
    """Serve transcription / speaker-tagged / corrected / cleaned / reformulated text.

    ``kind`` ∈ ``transcript`` | ``transcript-tagged`` | ``transcript-corrected``
              | ``transcript-cleaned`` | ``transcript-reformulated``
    ``ext``  ∈ ``txt`` | ``md`` | ``docx`` | ``odt``

    Returns 404 if the file isn't owned by the user, 503 if file-puller is
    unreachable, 410 if the requested output is empty (the corresponding
    sub-toggle was off or the LLM step failed).
    """
    if kind not in _TRANSCRIPT_KIND_TO_COLUMN:
        abort(404)
    if ext not in ("txt", "md", "docx", "odt"):
        abort(404)

    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj, audio = _audio_or_404(db, user["sub"], file_id)
        if audio is None:
            return jsonify({"error": "transcript_not_ready"}), 503
        column = _TRANSCRIPT_KIND_TO_COLUMN[kind]
        text = audio.get(column)
        if not text:
            return jsonify({"error": f"{kind}_unavailable"}), 410
        stem = _build_download_basename(file_obj, audio, kind)
        body = _maybe_prepend_key_points(text, audio.get("key_points_summary"), ext)
        if ext == "txt":
            return _send_text_attachment(body, f"{stem}.txt", "text/plain; charset=utf-8")
        if ext == "md":
            return _send_text_attachment(body, f"{stem}.md", "text/markdown; charset=utf-8")
        from app.transcript_formats import text_to_docx_bytes, text_to_odt_bytes
        if ext == "docx":
            blob = text_to_docx_bytes(body, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             as_attachment=True, download_name=f"{stem}.docx")
        if ext == "odt":
            blob = text_to_odt_bytes(body, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.oasis.opendocument.text",
                             as_attachment=True, download_name=f"{stem}.odt")
    finally:
        db.close()


@app.route("/api/file/meeting-cr/<ext>/<file_id>")
@require_auth
def api_file_meeting_cr_download(ext, file_id):
    """Serve the meeting analysis (5-section CR) in the requested format.

    ``ext`` ∈ ``json`` | ``md`` | ``docx`` | ``odt``.
    """
    if ext not in ("json", "md", "docx", "odt"):
        abort(404)
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj, audio = _audio_or_404(db, user["sub"], file_id)
        if audio is None:
            return jsonify({"error": "transcript_not_ready"}), 503
        raw = audio.get("meeting_analysis_json")
        if not raw:
            return jsonify({"error": "meeting_cr_unavailable"}), 410
        stem = _build_download_basename(file_obj, audio, "meeting-cr")
        if ext == "json":
            # Pass the JSON through directly so users can re-process it.
            return _send_text_attachment(raw, f"{stem}.json",
                                         "application/json; charset=utf-8")
        from app.transcript_formats import (
            meeting_analysis_to_markdown, text_to_docx_bytes, text_to_odt_bytes,
        )
        md = meeting_analysis_to_markdown(raw)
        if ext == "md":
            return _send_text_attachment(md, f"{stem}.md", "text/markdown; charset=utf-8")
        if ext == "docx":
            blob = text_to_docx_bytes(md, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             as_attachment=True, download_name=f"{stem}.docx")
        if ext == "odt":
            blob = text_to_odt_bytes(md, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.oasis.opendocument.text",
                             as_attachment=True, download_name=f"{stem}.odt")
    finally:
        db.close()


@app.route("/api/file/transcript-status/<file_id>")
@require_auth
def api_file_transcript_status(file_id):
    """Return which transcript outputs are available for a file (UI uses this
    to decide which download buttons to render). 404 if not owned, 200 with
    ``{"available": false}`` if the user_audio_files row doesn't exist yet.
    """
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj, audio = _audio_or_404(db, user["sub"], file_id)
        if audio is None:
            return jsonify({"available": False, "reason": "not_ready"})
        flags = {k: bool(audio.get(col)) for k, col in _TRANSCRIPT_KIND_TO_COLUMN.items()}
        flags["meeting-cr"] = bool(audio.get("meeting_analysis_json"))
        return jsonify({
            "available": True,
            "transcription_status": audio.get("transcription_status"),
            "transcription_engine": audio.get("transcription_engine"),
            "transcription_language": audio.get("transcription_language"),
            "outputs": flags,
            "suggested_filename": audio.get("suggested_filename"),
            "key_points_summary": audio.get("key_points_summary"),
            # Permet à l'UI de demander la position d'attente précise via
            # /api/queue-status?job_id=… plutôt que le générique
            # "N jobs en attente" (Phase 2 du sprint queue hint).
            "kevent_job_id": audio.get("kevent_job_id"),
        })
    finally:
        db.close()


@app.route("/api/file/download-transferred/<file_id>")
@require_auth
def api_file_download_transferred(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_transferred_storage(db, file_obj)
        if not cfg or not key:
            abort(404, "Transferred file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(
            data,
            mimetype=_guess_audio_mime_from_key(key),
            as_attachment=True,
            download_name=f"{Path(file_obj.original_filename).stem}_transferred{suffix}",
        )
    finally:
        db.close()


@app.route("/api/file/stream-transferred/<file_id>")
@require_auth
def api_file_stream_transferred(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = _resolve_transferred_storage(db, file_obj)
        if not cfg or not key:
            abort(404, "Transferred file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(
            data,
            mimetype=_guess_audio_mime_from_key(key),
            as_attachment=False,
            download_name=f"{Path(file_obj.original_filename).stem}_transferred{suffix}",
        )
    finally:
        db.close()


@app.route("/api/queue-status", methods=["GET"])
def api_queue_status():
    """Proxy live vers file-puller /api/v1/queue-status (qui parle au gateway).

    Accepte 2 modes d'auth :
      - session OIDC (utilisateur connecté à mydevices)
      - bearer INTERNAL_API_TOKEN (cross-cluster depuis upload-portal)

    Query: ``job_id`` (optionnel) pour position+ETA spécifique.
    Réponse: ``QueueSummary`` JSON (cf libs/shared/app/queue_eta.py).
    En cas d'erreur upstream → 200 + payload neutre (stale=true) plutôt
    que de propager l'erreur, pour que la UI puisse afficher
    "indisponible" sans casser.
    """
    # Auth dual : soit OIDC session, soit bearer INTERNAL_API_TOKEN.
    has_session = bool(session.get("user"))
    has_internal = verify_bearer_token(
        request.headers.get("Authorization", ""), INTERNAL_API_TOKEN
    )
    if not (has_session or has_internal):
        return jsonify({"error": "Unauthorized"}), 401
    job_id = (request.args.get("job_id") or "").strip()
    service_type = (request.args.get("service_type") or "audio").strip()
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL", "http://file-puller:8090").rstrip("/")
    params = {"service_type": service_type}
    if job_id:
        params["job_id"] = job_id
    try:
        resp = req.get(
            f"{base}/api/v1/queue-status",
            headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
            params=params,
            timeout=6,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception:
        logger.warning("queue-status proxy to file-puller failed", exc_info=True)
        from datetime import datetime, timezone as _tz
        return jsonify({
            "pending_total": None, "processing_total": None,
            "your_position": None, "eta_seconds": None,
            "throughput_per_min": None, "stale": True,
            "fetched_at": datetime.now(_tz.utc).isoformat(),
        }), 503


@app.route("/api/file/<file_id>/rename", methods=["POST"])
@require_auth
def api_rename_file(file_id):
    """Renomme le titre affiché (suggested_filename) d'un fichier.

    Body: ``{"title": "..."}``. Persiste via token-issuer
    ``POST /api/v1/files/by-session/rename`` (matche sur user_sub +
    simple_code + original_filename puisque les UUID externes/internes
    sont indépendants — cf. delete_file_by_session).
    """
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    new_title = (payload.get("title") or "").strip()
    if not new_title:
        return jsonify({"error": "title is required"}), 400
    if len(new_title) > 500:
        return jsonify({"error": "title too long"}), 400

    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            return jsonify({"error": "file_not_found"}), 404
        session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
        if not session_obj:
            return jsonify({"error": "session_not_found"}), 404
        try:
            data = request_internal_device_api(
                "POST",
                "/api/v1/files/by-session/rename",
                json_body={
                    "user_sub": user["sub"],
                    "simple_code": session_obj.simple_code,
                    "original_filename": file_obj.original_filename,
                    "new_title": new_title,
                },
            )
        except req.HTTPError as err:
            if err.response is not None:
                try:
                    body = err.response.json()
                except Exception:
                    body = {"error": "internal_api_error"}
                return jsonify({"error": body.get("error", "rename_failed")}), err.response.status_code
            return jsonify({"error": "rename_failed"}), 502
        return jsonify({"ok": True, "title": data.get("new_title", new_title)})
    finally:
        db.close()


@app.route("/api/my-trash", methods=["GET"])
@require_auth
def api_my_trash():
    """Liste les fichiers + sessions en corbeille de l'utilisateur.

    Inclut les items individuellement trashed et les sessions trashées
    (avec leurs fichiers). Au passage, déclenche le balayage opportuniste
    qui hard-delete les items > TRASH_RETENTION_DAYS.

    Réponse: { files: [{id, original_filename, simple_code, trashed_at,
    days_left}], sessions: [{simple_code, trashed_at, files: [...]}],
    retention_days: 30 }
    """
    user = get_current_user()
    db = SessionLocal()
    try:
        try:
            _purge_expired_trash(db, user["sub"])
            db.commit()
        except Exception:
            db.rollback()

        now = datetime.now(timezone.utc)
        # Fichiers individuels en corbeille (session active)
        files_q = (
            db.query(UploadedFile)
            .join(UploadSession, UploadSession.id == UploadedFile.session_id)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.is_(None),
                UploadedFile.trashed_at.isnot(None),
            )
            .order_by(UploadedFile.trashed_at.desc())
            .all()
        )
        files_list = [{
            "id": str(f.id),
            "original_filename": f.original_filename,
            "simple_code": f.session.simple_code if f.session else None,
            "trashed_at": f.trashed_at.isoformat() if f.trashed_at else None,
            "days_left": max(0, TRASH_RETENTION_DAYS - (now - f.trashed_at.replace(tzinfo=timezone.utc)).days)
                if f.trashed_at else None,
        } for f in files_q]

        # Sessions complètes en corbeille
        sessions_q = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.isnot(None),
            )
            .order_by(UploadSession.trashed_at.desc())
            .all()
        )
        sessions_list = [{
            "simple_code": s.simple_code,
            "id": str(s.id),
            "trashed_at": s.trashed_at.isoformat() if s.trashed_at else None,
            "days_left": max(0, TRASH_RETENTION_DAYS - (now - s.trashed_at.replace(tzinfo=timezone.utc)).days)
                if s.trashed_at else None,
            "files_count": len(s.uploads),
        } for s in sessions_q]

        return jsonify({
            "files": files_list,
            "sessions": sessions_list,
            "retention_days": TRASH_RETENTION_DAYS,
        })
    finally:
        db.close()


@app.route("/api/file/<file_id>/restore", methods=["POST"])
@require_auth
def api_restore_file(file_id):
    """Restaure un fichier de la corbeille — clear `trashed_at`."""
    user = get_current_user()
    db = SessionLocal()
    try:
        f = (
            db.query(UploadedFile)
            .join(UploadSession, UploadSession.id == UploadedFile.session_id)
            .filter(
                UploadedFile.id == file_id,
                UploadSession.user_sub == user["sub"],
                UploadedFile.trashed_at.isnot(None),
            )
            .first()
        )
        if not f:
            return jsonify({"error": "file_not_in_trash"}), 404
        f.trashed_at = None
        db.commit()
        return jsonify({"ok": True, "restored": True, "filename": f.original_filename})
    except Exception:
        db.rollback()
        logger.exception("Failed to restore file %s", file_id)
        return jsonify({"error": "restore_failed"}), 500
    finally:
        db.close()


@app.route("/api/my-sessions/<simple_code>/restore", methods=["POST"])
@require_auth
def api_restore_session(simple_code):
    """Restaure une session de la corbeille."""
    user = get_current_user()
    db = SessionLocal()
    try:
        s = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.simple_code == simple_code,
                UploadSession.trashed_at.isnot(None),
            )
            .first()
        )
        if not s:
            return jsonify({"error": "session_not_in_trash"}), 404
        s.trashed_at = None
        db.commit()
        return jsonify({"ok": True, "restored": True, "simple_code": simple_code})
    except Exception:
        db.rollback()
        logger.exception("Failed to restore session %s", simple_code)
        return jsonify({"error": "restore_failed"}), 500
    finally:
        db.close()


@app.route("/api/file/<file_id>/permanently", methods=["DELETE"])
@require_auth
def api_delete_file_permanently(file_id):
    """Hard-delete d'un fichier déjà en corbeille (depuis "Vider la corbeille"
    ou bouton "Supprimer définitivement"). Supprime DB + S3 + cleanup interne.
    """
    user = get_current_user()
    db = SessionLocal()
    deleted_objects = 0
    try:
        f = (
            db.query(UploadedFile)
            .join(UploadSession, UploadSession.id == UploadedFile.session_id)
            .filter(
                UploadedFile.id == file_id,
                UploadSession.user_sub == user["sub"],
                UploadedFile.trashed_at.isnot(None),
            )
            .first()
        )
        if not f:
            return jsonify({"error": "file_not_in_trash"}), 404
        session_obj = db.query(UploadSession).filter(UploadSession.id == f.session_id).first()
        simple_code = session_obj.simple_code if session_obj else None
        original_filename = f.original_filename
        for cfg, key in (
            (s3_upload_cfg, f.stored_filename),
            (s3_processed_cfg, f.transcoded_filename),
        ):
            try:
                if key:
                    delete_object(cfg, key)
                    deleted_objects += 1
            except Exception:
                logger.warning("Permanent delete: S3 fail %s", key, exc_info=True)
        try:
            t_cfg, t_key = _resolve_transferred_storage(db, f)
            if t_cfg and t_key:
                delete_object(t_cfg, t_key)
                deleted_objects += 1
        except Exception:
            logger.warning("Permanent delete: transferred fail", exc_info=True)
        db.delete(f)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Permanent delete file %s failed", file_id)
        return jsonify({"error": "delete_failed"}), 500
    finally:
        db.close()

    # Cleanup interne (best-effort)
    if simple_code and original_filename:
        try:
            request_internal_device_api(
                "DELETE", "/api/v1/files/by-session",
                json_body={
                    "user_sub": user.get("sub", ""),
                    "simple_code": simple_code,
                    "original_filename": original_filename,
                },
            )
        except Exception:
            logger.debug("Internal cleanup post-purge failed for %s", file_id, exc_info=True)

    return jsonify({"ok": True, "deleted_objects": deleted_objects})


@app.route("/api/file/<file_id>", methods=["DELETE"])
@require_auth
def api_delete_file(file_id):
    """Soft-delete (corbeille) : positionne trashed_at sur le fichier.

    Le fichier disparaît de la liste mydevices immédiatement, mais reste
    en DB + S3 pendant TRASH_RETENTION_DAYS (30j par défaut). À l'expiration,
    `_purge_expired_trash` (déclenché par le prochain GET /api/my-sessions
    de l'utilisateur) fait le hard-delete S3 + DB + zone interne.

    Ownership vérifiée via _get_owned_file (qui filtre déjà les trashed).
    """
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            return jsonify({"error": "file_not_found"}), 404
        file_obj.trashed_at = datetime.now(timezone.utc)
        original_filename = file_obj.original_filename
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to trash file %s for user %s", file_id, user.get("sub"))
        return jsonify({"error": "file_delete_failed"}), 500
    finally:
        db.close()

    return jsonify({
        "ok": True,
        "trashed": True,
        "retention_days": TRASH_RETENTION_DAYS,
        "filename": original_filename,
    })


@app.route("/api/my-sessions/<simple_code>", methods=["DELETE"])
@require_auth
def api_delete_session(simple_code):
    """Soft-delete (corbeille) d'une session entière.

    Positionne `trashed_at` sur la session : elle disparaît de la liste
    et ses fichiers ne sont plus accessibles (download/transcript). La
    purge définitive (DB + S3 + cleanup interne via token-issuer) est faite
    par `_purge_expired_trash` après TRASH_RETENTION_DAYS.
    """
    user = get_current_user()
    db = SessionLocal()
    try:
        s = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.simple_code == simple_code,
                UploadSession.trashed_at.is_(None),
            )
            .first()
        )
        if not s:
            return jsonify({"error": "session_not_found"}), 404
        s.trashed_at = datetime.now(timezone.utc)
        deleted_files = sum(1 for _ in s.uploads)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to trash session %s for user %s", simple_code, user.get("sub"))
        return jsonify({"error": "session_delete_failed"}), 500
    finally:
        db.close()

    return jsonify({
        "ok": True,
        "trashed": True,
        "retention_days": TRASH_RETENTION_DAYS,
        "deleted_files": deleted_files,
    })


@app.route("/api/purge-my-sessions", methods=["POST"])
@require_auth
def api_purge_my_sessions():
    """Soft-delete (corbeille) de TOUTES les sessions de l'utilisateur.

    Le hard-delete (DB + S3) sera fait par `_purge_expired_trash` après
    TRASH_RETENTION_DAYS. Idempotent : seules les sessions actuellement
    visibles (non déjà en corbeille) sont marquées.
    """
    user = get_current_user()
    db = SessionLocal()
    deleted_sessions = 0
    deleted_files = 0
    try:
        now = datetime.now(timezone.utc)
        sessions = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.is_(None),
            )
            .all()
        )
        for s in sessions:
            s.trashed_at = now
            deleted_files += sum(1 for _ in s.uploads)
            deleted_sessions += 1
        db.commit()
        return jsonify({
            "ok": True,
            "trashed": True,
            "retention_days": TRASH_RETENTION_DAYS,
            "deleted_sessions": deleted_sessions,
            "deleted_files": deleted_files,
        })
    except Exception:
        db.rollback()
        logger.exception("Failed to trash user sessions for %s", user["sub"])
        return jsonify({"error": "Failed to purge sessions"}), 500
    finally:
        db.close()


@app.route("/api/file/normalization-impact/<file_id>")
@require_auth
def api_file_normalization_impact(file_id):
    user = get_current_user()
    db = SessionLocal()
    try:
        file_obj = _get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        if not file_obj.transcoded_filename:
            return jsonify({"error": "Fichier pas encore transcodé"}), 400

        # Chemin rapide : si transcode-worker a déjà persisté les mesures
        # (source + output), on les lit directement, plus de redownload S3.
        if (file_obj.normalization_source_i is not None
                and file_obj.normalization_output_i is not None):
            source = {
                "i":   file_obj.normalization_source_i,
                "tp":  file_obj.normalization_source_tp,
                "lra": file_obj.normalization_source_lra,
            }
            normalized = {
                "i":   file_obj.normalization_output_i,
                "tp":  file_obj.normalization_output_tp,
                "lra": file_obj.normalization_output_lra,
            }
            target_i = -16.0
            source_dist = abs(source["i"] - target_i)
            normalized_dist = abs(normalized["i"] - target_i)
            improvement = round(source_dist - normalized_dist, 2)
            return jsonify({
                "target": {"i": target_i, "tp": -1.5, "lra": 11.0},
                "source": source,
                "normalized": normalized,
                "delta": {
                    "i":   round(normalized["i"] - source["i"], 2),
                    "tp":  round(normalized["tp"] - source["tp"], 2),
                    "lra": round(normalized["lra"] - source["lra"], 2),
                },
                "improvement_to_target_lufs": improvement,
                "from_cache": True,
            })

        # Fallback : pour les fichiers d'avant la migration (mesures non
        # persistées), on tente le calcul live. Si la source S3 a déjà été
        # purgée → 410 explicite.
        try:
            if not object_exists(s3_upload_cfg, file_obj.stored_filename or ""):
                return jsonify({
                    "error": "Source audio purgée (la mesure n'était pas "
                             "persistée pour ce fichier ; les nouveaux uploads "
                             "auront les valeurs disponibles directement)."
                }), 410
        except Exception:
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            src_suffix = Path(file_obj.stored_filename or "").suffix or ".audio"
            out_suffix = Path(file_obj.transcoded_filename or "").suffix or ".wav"
            src_path = os.path.join(tmpdir, f"source{src_suffix}")
            out_path = os.path.join(tmpdir, f"normalized{out_suffix}")

            with open(src_path, "wb") as src_f:
                src_f.write(download_fileobj(s3_upload_cfg, file_obj.stored_filename).read())
            with open(out_path, "wb") as out_f:
                out_f.write(download_fileobj(s3_processed_cfg, file_obj.transcoded_filename).read())

            source = _run_loudnorm_measure(src_path)
            normalized = _run_loudnorm_measure(out_path)

        target_i = -16.0
        source_dist = abs(source["i"] - target_i)
        normalized_dist = abs(normalized["i"] - target_i)
        improvement = round(source_dist - normalized_dist, 2)

        return jsonify({
            "target": {"i": target_i, "tp": -1.5, "lra": 11.0},
            "source": source,
            "normalized": normalized,
            "delta": {
                "i": round(normalized["i"] - source["i"], 2),
                "tp": round(normalized["tp"] - source["tp"], 2),
                "lra": round(normalized["lra"] - source["lra"], 2),
            },
            "improvement_to_target_lufs": improvement,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout analyse audio"}), 504
    except Exception:
        logger.exception("Failed to compute normalization impact for file %s", file_id)
        return jsonify({"error": "Analyse indisponible"}), 500
    finally:
        db.close()


# ─── HTML Template ──────────────────────────────────────────

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MIrAI — Enregistrer, transcrire et analyser vos réunions et notes vocales en toute sécurité</title>
    <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/pwa-icon-192.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/icons/pwa-icon-180.png">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14.2/dist/dsfr/dsfr.min.css">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14.2/dist/utility/icons/icons-system/icons-system.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: #f6f6f6; color: #161616; min-height: 100vh;
        }
        .page-shell { padding: 1.5rem 0 2rem; }
        .container { max-width: 760px; width: 100%; }
        .card {
            background: #fff; border-radius: 0.5rem; padding: 1.5rem;
            border: 1px solid #e5e5e5; margin-bottom: 1rem;
        }
        h1 { font-size: 1.3rem; margin-bottom: 0.5rem; color: #1a1a2e; }
        .subtitle { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
        .header-user {
            margin-left: auto;
            text-align: right;
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 0.15rem;
        }
        .header-user-name {
            font-size: 0.86rem;
            font-weight: 600;
            color: #161616;
            line-height: 1.2;
            max-width: 17rem;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .header-user .fr-link {
            font-size: 0.8rem;
            line-height: 1.2;
        }
        /* Toggle "Mode avancé" sous le bouton Déconnexion. Pill compacte
           qui change d'état visuel quand actif. Astuce power-user :
           maintenir Alt fait peek temporairement vers le mode avancé
           sans changer le toggle (état restauré au relâchement). */
        .header-user .advanced-toggle {
            margin-top: 0.15rem;
            font-size: 0.7rem; line-height: 1;
            padding: 0.18rem 0.5rem;
            background: transparent; color: #64748b;
            border: 1px solid #cbd5e1; border-radius: 999px;
            cursor: pointer; user-select: none;
            transition: all 0.12s ease;
        }
        .header-user .advanced-toggle:hover {
            background: #f1f5f9; color: #0f172a; border-color: #94a3b8;
        }
        .header-user .advanced-toggle.is-on {
            background: #1e293b; color: #fff; border-color: #1e293b;
        }
        /* Pendant le peek Alt : on souligne le toggle pour indiquer que
           c'est temporaire (n'a pas changé l'état persistant). */
        .header-user .advanced-toggle.is-peek {
            box-shadow: 0 0 0 2px #fde68a;
        }
        .form-group { margin-bottom: 1rem; }
        label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.3rem; color: #444; }
        select, input[type=number] {
            width: 100%; padding: 0.6rem; border: 1px solid #ddd; border-radius: 8px;
            font-size: 0.95rem; background: white;
        }
        .btn-primary {
            width: auto;
            padding: 0.45rem 0.75rem;
            border: none;
            border-radius: 8px;
            font-size: 0.86rem;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
            display: inline-flex;
            align-items: center;
            justify-content: center;
        }
        .btn-primary:disabled { background: #94a3b8; cursor: not-allowed; }
        .btn-danger-mini {
            width: auto;
            min-height: 1.1rem;
            padding: 0.06rem 0.32rem;
            background: #b86464 !important;
            color: #ffffff !important;
            border: 1px solid #a95959 !important;
            font-size: 0.62rem;
            line-height: 1;
            border-radius: 6px;
        }
        .btn-danger-mini:hover,
        .btn-danger-mini.fr-btn:hover,
        .btn-danger-mini.fr-btn:active,
        .btn-danger-mini.fr-btn:focus {
            background: #ab5c5c !important;
            border-color: #9d5050 !important;
            color: #ffffff !important;
            box-shadow: none;
        }
        .btn-danger-mini:disabled { background: #cbd5e1; color: #64748b; cursor: not-allowed; }
        .btn-renew-mini {
            min-height: 1.38rem !important;
            padding: 0.08rem 0.42rem !important;
            font-size: 0.62rem !important;
            line-height: 1 !important;
            border-radius: 6px !important;
            margin-left: 0.35rem;
        }
        .btn-renew-alert {
            animation: renewPulse 1.15s ease-in-out infinite;
            box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.45);
        }
        @keyframes renewPulse {
            0% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.45); }
            70% { box-shadow: 0 0 0 6px rgba(245, 158, 11, 0); }
            100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
        }
        .btn-rename-mini {
            min-height: 0.8rem !important;
            padding: 0.04rem 0.24rem !important;
            font-size: 0.62rem !important;
            line-height: 1 !important;
            border-radius: 6px !important;
        }
        .device-filter-btn {
            font-size: 0.64rem !important;
            min-height: 1.05rem !important;
            padding: 0.04rem 0.34rem !important;
            line-height: 1 !important;
            border-radius: 5px !important;
        }
        .device-row-revoked {
            background: #f1f5f9;
            border-color: #cbd5e1 !important;
            opacity: 0.92;
        }
        .device-row-revoked .device-name {
            color: #475569 !important;
        }
        .device-row-revoked .device-meta {
            color: #64748b !important;
        }
        .result { display: none; text-align: center; }
        .result.active { display: block; }
        .simple-code {
            font-size: 2.5rem; font-weight: 800; letter-spacing: 0.3em;
            color: #2563eb; margin: 1rem 0; font-family: 'Courier New', monospace;
        }
        .qr-container { margin: 1rem auto; }
        .qr-container img { border-radius: 8px; }
        .expires { color: #888; font-size: 0.85rem; margin-top: 0.5rem; }
        .sessions-list { margin-top: 1rem; }
        .sessions-list {
            max-height: 420px;
            overflow-y: auto;
            padding-right: 0.25rem;
        }
        /* En vue détail (page-mode), on supprime le scroll interne : la
           fiche occupe toute la hauteur naturelle, le scroll vit au niveau
           de la page (plus naturel sur mobile et desktop). */
        .tab-pane[data-tab="transfers"].detail-active .sessions-list {
            max-height: none; overflow: visible; padding-right: 0;
        }
        .session-item {
            padding: 0.75rem; background: #f8f9fa; border-radius: 8px;
            margin-bottom: 0.5rem; font-size: 0.85rem;
        }
        .session-item .code { font-weight: 700; font-family: monospace; color: #2563eb; }
        .device-token-code { font-weight: 700; font-family: monospace; color: #2563eb; }
        .status-badge {
            display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px;
            font-size: 0.75rem; font-weight: 600;
        }
        .status-active { background: #d1fae5; color: #065f46; }
        .status-expired { background: #fee2e2; color: #991b1b; }
        .file-badge-pending { background: #e5e7eb; color: #374151; }
        .file-badge-scanning { background: #dbeafe; color: #1d4ed8; }
        .file-badge-scan_clean { background: #dcfce7; color: #166534; }
        .file-badge-scan_infected { background: #fee2e2; color: #b91c1c; }
        .file-badge-transcoding { background: #ede9fe; color: #6d28d9; }
        .file-badge-transcoded { background: #e0f2fe; color: #075985; }
        .file-badge-ready_for_transfer { background: #fef3c7; color: #92400e; }
        .file-badge-transferring { background: #ffedd5; color: #9a3412; }
        .file-badge-transferred { background: #ccfbf1; color: #0f766e; }
        .file-badge-quarantined { background: #fecaca; color: #991b1b; }
        .file-badge-transcode_failed { background: #ffe4e6; color: #9f1239; }
        .file-badge-error { background: #f3f4f6; color: #7f1d1d; }
        .file-status { margin-top: 0.3rem; padding-left: 1rem; color: #555; }
        .file-status {
            margin-top: 0.35rem; padding: 0.55rem 0.65rem;
            background: #f8f9fa; border-radius: 8px; color: #444;
        }
        .file-name {
            display: inline-block;
            max-width: 230px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            vertical-align: bottom;
        }
        .file-links a { margin-right: 0.6rem; font-size: 0.78rem; }
        .file-links .link-disabled {
            margin-right: 0.6rem;
            font-size: 0.78rem;
            color: #94a3b8;
            text-decoration: line-through;
            cursor: not-allowed;
        }
        .transcript-section { margin-top: 0.15rem; padding-top: 0.2rem; border-top: 1px dashed #e2e8f0; }
        .transcript-status-line { display: flex; align-items: center; gap: 0.4rem; font-size: 0.78rem; color: #475569; margin-bottom: 0.35rem; padding: 0.3rem 0.5rem; border-radius: 6px; border: 1px solid transparent; }
        .transcript-status-line.transcript-status-error {
            background: #fef2f2; border-color: #fecaca; color: #991b1b;
            font-weight: 600;
        }
        .transcript-status-line.transcript-status-ok {
            background: #f0fdf4; border-color: #bbf7d0; color: #166534;
        }
        .transcript-status-spinner { width: 0.7rem; height: 0.7rem; border-radius: 50%; flex-shrink: 0; }
        .transcript-status-spinner.on { background: conic-gradient(#3b7dd8 0%, #3b7dd8 25%, transparent 25%, transparent 100%); animation: transcriptSpin 1.1s linear infinite; }
        .transcript-status-spinner.off { background: #94a3b8; }
        .transcript-status-spinner.err { background: #ef4444; }
        .transcript-status-spinner.ok { background: #10b981; }
        .transcript-status-icon { font-size: 0.95rem; line-height: 1; }
        /* Dropdown unifié des téléchargements (audios + transcripts) */
        /* Bloc téléchargements : liste verticale de "types de document", chaque
           ligne montrant à droite des boutons-icône (un par format). Objectif :
           pas de jargon dans la liste (que des types) + icônes facilement
           identifiables pour un néophyte. */
        .downloads-block {
            display: flex; flex-direction: column; gap: 0.15rem;
            margin-top: 0.4rem;
            border: 1px solid #e2e8f0; border-radius: 8px;
            background: #fbfcfd; padding: 0.35rem 0.5rem;
        }
        .downloads-row {
            display: flex; align-items: center; justify-content: space-between;
            gap: 0.5rem; padding: 0.08rem 0.2rem;
            border-bottom: 1px solid #f1f5f9;
            min-height: 1.8rem;
        }
        .downloads-row:last-child { border-bottom: 0; }
        .downloads-row-label {
            font-size: 0.82rem; color: #1e293b; flex: 1; min-width: 0;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .downloads-row-icons {
            display: flex; gap: 0.2rem; flex-shrink: 0;
        }
        /* Icônes de téléchargement : bouton minimaliste — juste l'icône,
           plus grosse, pas de chrome (border/bg) au repos. Hover = scale
           up + couleur ; active (clic) = scale down + flash background
           pour un feedback haptique-like. */
        .downloads-icon-btn {
            display: inline-flex; align-items: center; justify-content: center;
            width: 1.75rem; height: 1.75rem;
            border: 0; border-radius: 6px;
            background: transparent; color: #64748b;
            text-decoration: none; cursor: pointer;
            transition: transform 0.12s ease, color 0.12s ease, background 0.12s ease;
        }
        .downloads-icon-btn svg { width: 1.4rem; height: 1.4rem; }
        /* DSFR injecte une flèche "lien externe" sur les <a target="_blank">
           via ::after. On la retire pour nos boutons-icône — l'icône SVG
           porte déjà tout le sens visuel. */
        .downloads-icon-btn::after,
        .downloads-icon-btn::before { content: none !important; display: none !important; }
        .downloads-icon-btn { background-image: none !important; }
        .downloads-icon-btn:hover {
            color: #1d4ed8;
            transform: scale(1.18);
        }
        .downloads-icon-btn:active,
        .downloads-icon-btn.is-clicked {
            transform: scale(0.92);
            background: #dbeafe;
            color: #1e3a8a;
        }
        .downloads-icon-btn-play:hover { color: #166534; }
        .downloads-icon-btn-play:active,
        .downloads-icon-btn-play.is-clicked {
            background: #dcfce7; color: #14532d;
        }
        /* Feedback flash : ajouté en JS sur tout clic, retiré après 350ms. */
        @keyframes dlClickFlash {
            0%   { box-shadow: 0 0 0 0 rgba(59, 130, 246, 0.45); }
            100% { box-shadow: 0 0 0 12px rgba(59, 130, 246, 0);  }
        }
        .downloads-icon-btn.is-clicked { animation: dlClickFlash 0.45s ease-out; }
        /* Section "Autres téléchargements" : visuellement DISTINCTE des
           lignes par défaut — fond légèrement plus foncé, label "Autres :"
           à gauche, select étroit, icônes à droite. Séparée du bloc
           principal par un trait fin. */
        .downloads-other-row {
            display: flex; align-items: center;
            gap: 0.45rem; padding: 0.35rem 0.5rem;
            margin: 0.4rem -0.5rem -0.35rem -0.5rem;  /* étend au bord du bloc */
            border-top: 1px solid #e2e8f0;
            background: #f1f5f9;
            border-radius: 0 0 8px 8px;  /* coins arrondis bas comme le block */
        }
        .downloads-other-label {
            font-size: 0.72rem; color: #475569; font-weight: 600;
            white-space: nowrap;
            text-transform: uppercase; letter-spacing: 0.04em;
        }
        .downloads-other-select {
            flex: 0 1 45%; max-width: 45%; min-width: 0;
            padding: 0.12rem 0.4rem; font-size: 0.72rem;
            height: 1.65rem; line-height: 1.2;
            border: 1px solid #94a3b8; border-radius: 5px; background: #fff;
            color: #1e293b;
            background-position: right 0.35rem center;
        }
        .downloads-other-icons {
            margin-left: auto;       /* pousse les icônes contre le bord droit */
            min-width: 2.4rem;       /* réserve la place même quand vide */
            min-height: 1.85rem;
            display: inline-flex; gap: 0.2rem;
        }
        .file-impact-row { margin-top: 0.35rem; display: flex; align-items: center; gap: 0.5rem; }
        /* Diagnostic per-step de la transcription IA (dépliable) */
        .status-details {
            margin: 0.4rem 0 0.6rem 0; font-size: 0.78rem;
            border: 1px solid #e2e8f0; border-radius: 6px;
            background: #fbfcfd;
        }
        .status-details > summary {
            cursor: pointer; padding: 0.45rem 0.6rem;
            color: #1d4ed8; font-weight: 500;
        }
        .status-details > summary:hover { background: #f1f5f9; }
        .status-step-list { padding: 0 0.6rem 0.6rem 0.6rem; }
        .status-step {
            display: grid; grid-template-columns: 1rem 1fr;
            grid-template-rows: auto auto;
            gap: 0.1rem 0.5rem; padding: 0.35rem 0;
            border-bottom: 1px dashed #e2e8f0;
        }
        .status-step:last-child { border-bottom: 0; }
        .status-step > :first-child { grid-row: 1 / 3; font-size: 1rem; line-height: 1; padding-top: 0.05rem; }
        .status-step-label { font-weight: 600; color: #0f172a; }
        .status-step-desc { color: #64748b; font-size: 0.74rem; }
        /* Bouton ⓘ aligné à droite, prend couleur du status */
        /* Bouton (i) : juste un petit rond neutre avec un i dedans.
           La couleur ne reflète plus le statut (la pastille colorée porte
           déjà cette info dans la liste compact). */
        .file-detail-info-btn {
            margin-left: auto;
            width: 1.4rem; height: 1.4rem; padding: 0;
            display: inline-flex; align-items: center; justify-content: center;
            border: 1.5px solid #94a3b8; background: #fff;
            cursor: pointer; font-size: 0.78rem; font-weight: 700;
            font-style: italic; font-family: Georgia, serif;
            line-height: 1; border-radius: 50%; color: #475569;
        }
        .file-detail-info-btn:hover {
            background: #f1f5f9; border-color: #475569; color: #0f172a;
        }
        /* Pulse "il se passe un truc" sur le bouton (i) tant que le pipeline
           (upload + transcription) n'est pas terminal. Sync avec le dot. */
        @keyframes infoBtnPulse {
            0%, 100% { box-shadow: 0 0 0 0 rgba(59,130,246,0.55); }
            50%      { box-shadow: 0 0 0 6px rgba(59,130,246,0);  }
        }
        .file-detail-info-btn.is-in-progress {
            border-color: #3b82f6; color: #1d4ed8;
            animation: infoBtnPulse 1.4s ease-in-out infinite;
        }
        /* Modal détails techniques : fenêtre flottante positionnée vers le
           haut (haut de page reste visible). Backdrop très léger. */
        .file-info-modal {
            position: fixed; top: 3.5rem; left: 50%;
            transform: translateX(-50%);
            margin: 0;
            border: 1px solid #cbd5e1; border-radius: 12px; padding: 0;
            max-width: 560px; width: 92%;
            max-height: calc(100vh - 5rem); overflow-y: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.22),
                        0 0 0 1px rgba(15,23,42,0.05);
            background: #fff;
        }
        .file-info-modal::backdrop {
            background: rgba(15,23,42,0.18);
        }
        .modal-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 0.8rem 1rem; border-bottom: 1px solid #e2e8f0;
        }
        .modal-header h3 { margin: 0; font-size: 1rem; color: #0f172a; }
        .modal-close {
            border: 0; background: transparent; cursor: pointer;
            font-size: 1rem; color: #64748b; padding: 0.2rem 0.4rem;
            border-radius: 4px;
        }
        .modal-close:hover { background: #e2e8f0; color: #0f172a; }
        .modal-body { padding: 0.8rem 1rem 1rem 1rem; font-size: 0.84rem; }
        .modal-section { margin-bottom: 1rem; }
        .modal-section:last-child { margin-bottom: 0; }
        .modal-section-title {
            font-weight: 700; color: #0f172a; font-size: 0.82rem;
            margin-bottom: 0.4rem; text-transform: uppercase;
            letter-spacing: 0.04em; color: #64748b;
        }
        .modal-status { margin-bottom: 0.3rem; }
        .modal-steps { display: flex; flex-direction: column; gap: 0.5rem; }
        .modal-step {
            display: grid; grid-template-columns: 1rem 1fr;
            gap: 0.5rem; align-items: start;
            padding: 0.3rem 0; border-bottom: 1px dashed #e2e8f0;
        }
        .modal-step:last-child { border-bottom: 0; }
        .modal-step-label { font-weight: 600; color: #0f172a; }
        .modal-step-desc { color: #64748b; font-size: 0.76rem; line-height: 1.35; }
        .modal-explain {
            margin: 0 0 0.5rem 0; color: #475569; font-size: 0.78rem; line-height: 1.4;
        }
        .modal-impact-result { margin-top: 0.5rem; font-size: 0.78rem; color: #475569; }
        /* #3 Détail full-height : le panel détail prend toute la hauteur
           dispo, le scroll est celui de la page entière, pas un scroll
           interne secondaire. */
        .tab-pane[data-tab="transfers"].detail-active .card {
            border: 0; box-shadow: none; padding: 0.4rem 0.2rem;
            background: transparent;
        }
        .tab-pane[data-tab="transfers"].detail-active #recent-activities-panel {
            min-height: calc(100vh - 180px);
        }
        /* Impact normalisation audio (dépliable, en vue détail) */
        .impact-details {
            margin: 0.6rem 0; font-size: 0.78rem;
            border: 1px solid #e2e8f0; border-radius: 6px;
            background: #fbfcfd;
        }
        .impact-details > summary {
            cursor: pointer; padding: 0.45rem 0.6rem;
            color: #1d4ed8; font-weight: 500;
        }
        .impact-details > summary:hover { background: #f1f5f9; }
        .impact-explanation {
            margin: 0; padding: 0 0.6rem 0.5rem 0.6rem;
            color: #475569; font-size: 0.76rem; line-height: 1.4;
        }
        .impact-details .file-impact-row { padding: 0 0.6rem 0.6rem 0.6rem; margin: 0; }
        .impact-hint { font-size: 0.72rem; color: #94a3b8; }
        /* Hint file d'attente Kevent — sobre, atténuée si stale */
        .queue-hint { color: #475569; }
        .queue-hint.queue-hint-stale { color: #94a3b8; font-style: italic; }
        /* Corbeille */
        .trash-item {
            display: flex; flex-wrap: wrap; align-items: center; gap: 0.55rem;
            padding: 0.5rem 0.6rem; border-bottom: 1px solid #f1f5f9;
        }
        .trash-item-type {
            font-size: 0.7rem; color: #64748b; text-transform: uppercase;
            background: #f1f5f9; padding: 0.1rem 0.4rem; border-radius: 4px;
        }
        .trash-item-name { flex: 1; min-width: 0; font-size: 0.85rem; }
        .trash-item-meta { font-size: 0.72rem; color: #94a3b8; }
        /* ── Vue LISTE COMPACTE — une ligne par fichier ───────────── */
        .file-row-compact-wrapper { border-bottom: 1px solid #f1f5f9; }
        .file-row-compact-wrapper:hover { background: #f8fafc; }
        .file-row-compact {
            display: grid;
            grid-template-columns: auto auto minmax(0,1fr) auto auto auto;
            align-items: center; gap: 0.55rem;
            padding: 0.45rem 0.55rem;
            min-height: 32px;
        }
        /* Dot caractère unicode "●" : aligné naturellement sur la
           baseline du texte (pas de div/flex contournant). Sa couleur
           est ajustée par loadTranscriptStatus selon le statut.
           Approche cleanup : on s'appuie sur la métrique font, pas
           sur un cercle CSS qui retombait toujours plus bas que le
           texte (les techniques flex/inline-flex ne fixent pas). */
        .file-row-dot {
            font-size: 0.7rem; line-height: 1; color: #cbd5e1;
            user-select: none;
        }
        .file-row-dot-completed,
        .file-row-dot-kevent_completed,
        .file-row-dot-mcr_pushed { color: #10b981; }
        .file-row-dot-kevent_partially_completed { color: #f59e0b; }
        .file-row-dot-failed,
        .file-row-dot-kevent_failed,
        .file-row-dot-mcr_auth_failed,
        .file-row-dot-mcr_rejected,
        .file-row-dot-mcr_push_failed { color: #b91c1c; }
        .file-row-dot-pending,
        .file-row-dot-processing,
        .file-row-dot-kevent_queued,
        .file-row-dot-kevent_transcribing,
        .file-row-dot-kevent_processing,
        .file-row-dot-upload-in-progress { color: #3b82f6; }
        .file-row-dot-disabled { color: #94a3b8; }
        /* Animation "il se passe un truc" : pulse opacity+scale tant que
           le pipeline (upload + transcription) n'est pas terminal. */
        @keyframes filerowDotPulse {
            0%, 100% { opacity: 1;   transform: scale(1);    }
            50%      { opacity: 0.4; transform: scale(1.35); }
        }
        .file-row-dot-pending,
        .file-row-dot-processing,
        .file-row-dot-kevent_queued,
        .file-row-dot-kevent_transcribing,
        .file-row-dot-kevent_processing,
        .file-row-dot-upload-in-progress {
            animation: filerowDotPulse 1.4s ease-in-out infinite;
            display: inline-block; /* nécessaire pour que transform: scale prenne effet */
        }
        .file-row-title {
            font-weight: 600; color: #1d4ed8;
            text-decoration: none !important;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            min-width: 0; line-height: 1.2;
            background-image: none !important; /* DSFR ajoute parfois un underline via gradient */
        }
        .file-row-title:hover {
            color: #1e40af; text-decoration: none !important;
        }
        .file-row-meta {
            font-size: 0.75rem; color: #64748b; white-space: nowrap;
            display: inline-flex; flex-direction: column; align-items: flex-end;
            line-height: 1.15; gap: 0.05rem;
        }
        .file-row-meta .file-row-meta-date { font-weight: 500; color: #475569; }
        .file-row-meta .file-row-meta-dur { color: #94a3b8; font-variant-numeric: tabular-nums; }
        /* Hint file d'attente Kevent (liste compacte). Invisible quand vide
           (CSS :empty), discret quand peuplé. */
        .file-row-queue-hint {
            font-size: 0.72rem; color: #2563eb; font-style: italic;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            min-width: 0;
        }
        .file-row-queue-hint:empty { display: none; }
        .file-row-queue-hint.queue-hint-stale { color: #94a3b8; }
        /* Bandeau d'alerte virus inline (B1) — affiché en tête de la file-row
           ou de la vue détail quand f.status ∈ {scan_infected, quarantined}.
           Couleur rouge appuyée, icône ⚠, message explicite. */
        .file-row-virus-banner {
            display: flex; align-items: center; gap: 0.55rem;
            padding: 0.5rem 0.75rem; margin: 0.2rem 0.3rem 0.3rem 0.3rem;
            background: #fee2e2; border: 1px solid #fca5a5;
            border-left: 4px solid #b91c1c; border-radius: 6px;
            color: #7f1d1d; font-size: 0.82rem; line-height: 1.35;
        }
        .file-row-virus-icon {
            font-size: 1.1rem; color: #b91c1c; flex-shrink: 0;
        }
        .file-row-virus-msg strong { color: #b91c1c; }
        /* Quand virus bloqué : la row entière prend un fond rose pâle pour
           bien marquer la mise en quarantaine. */
        .file-row-virus { background: #fef2f2; }
        .file-row-virus .file-row-title {
            color: #7f1d1d !important;
            text-decoration: line-through !important;
        }
        .file-detail-virus { background: #fef2f2; padding: 0.4rem; border-radius: 8px; }
        /* Dot couleur virus (rouge fixe, pas d'animation pulse). */
        .file-row-dot-scan_infected,
        .file-row-dot-quarantined { color: #b91c1c !important; animation: none !important; }
        /* En vue détail (queue-hint au-dessus du rail), le widget partage
           la classe queue-hint. On garde son style existant intact. */
        .queue-hint:empty { display: none; }
        /* Nom du fichier audio d'origine à droite de date+durée (vue détail),
           légèrement bleuté. Aligné horizontalement avec date/durée. */
        .file-detail-source-filename {
            font-size: 0.74rem; color: #3b82f6; font-style: italic;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            flex: 1; min-width: 0; text-align: left;
            margin-left: 0.4rem;
        }
        /* En vue détail (techline), la date+durée s'affiche EN LIGNE
           (pas en colonne empilée comme dans la liste). On surcharge
           .file-row-meta pour ce contexte uniquement. */
        .file-detail-techline .file-row-meta {
            flex-direction: row; align-items: baseline; gap: 0.4rem;
        }
        .file-detail-techline .file-row-meta-dur::before,
        .file-detail-techline .file-detail-source-filename::before {
            content: "•"; color: #cbd5e1; margin-right: 0.4rem;
        }
        /* Bouton icône (poubelle, camion poubelle) : remplace les boutons texte
           Supprimer/Purger. Fond transparent, hover discret, tooltip natif. */
        .icon-btn {
            background: transparent; border: 1px solid transparent;
            cursor: pointer; padding: 0.18rem 0.32rem; border-radius: 6px;
            line-height: 1; color: #64748b;
            display: inline-flex; align-items: center; justify-content: center;
        }
        .icon-btn svg { width: 1.05rem; height: 1.05rem; }
        .icon-btn:hover { background: #fee2e2; color: #b91c1c; border-color: #fecaca; }
        .icon-btn:focus { outline: 2px solid #fca5a5; outline-offset: 1px; }
        .icon-btn[disabled] { opacity: 0.35; cursor: not-allowed; }
        .icon-btn[disabled]:hover { background: transparent; color: #64748b; border-color: transparent; }
        /* Variante "purge" plus marquée — camion poubelle, action lourde */
        .icon-btn-purge { color: #b91c1c; }
        .icon-btn-purge svg { width: 1.7rem; height: 1.3rem; }
        .icon-btn-purge:hover { background: #b91c1c; color: #fff; border-color: #991b1b; }
        /* Mini-poubelle qui s'élève + bascule au hover du bouton purge —
           petite touche humoristique d'éboueurs au travail. */
        .icon-btn-purge .purge-bin {
            transition: transform 0.5s cubic-bezier(0.4, 0, 0.2, 1);
            transform-origin: 4px 17px;
        }
        .icon-btn-purge:hover .purge-bin {
            transform: translate(8px, -4px) rotate(75deg);
        }
        .icon-btn-purge .purge-workers {
            transition: opacity 0.3s ease;
            opacity: 0.7;
        }
        .icon-btn-purge:hover .purge-workers { opacity: 1; }
        /* Mode avancé : .advanced-only n'est visible que si body.adv-mode est posée. */
        .advanced-only { display: none !important; }
        body.adv-mode .advanced-only { display: inline-flex !important; }
        .file-row-expand {
            border: 1px solid transparent; background: transparent;
            cursor: pointer; color: #475569; font-size: 0.78rem;
            padding: 0.15rem 0.45rem; border-radius: 4px;
            display: inline-flex; align-items: center; gap: 0.25rem;
            white-space: nowrap;
        }
        .file-row-expand:hover { background: #e2e8f0; color: #0f172a; border-color: #cbd5e1; }
        .file-row-expand-icon { display: inline-block; transition: transform 0.15s ease; }
        .file-row-expand.is-open .file-row-expand-icon { transform: rotate(90deg); }
        .file-row-delete { white-space: nowrap; }
        .file-row-expanded {
            padding: 0.4rem 0.7rem 0.55rem 1.8rem;
            background: #f8fafc; border-top: 1px dashed #e2e8f0;
            font-size: 0.8rem;
        }
        .file-row-expanded-status {
            font-weight: 500; color: #475569; margin-bottom: 0.3rem;
        }
        .file-row-expanded-empty { color: #94a3b8; font-style: italic; }
        /* En mode compact (data-compact=1) on réduit le bandeau statut
           à un simple point coloré avec tooltip (rollover), on cache le
           résumé (visible seulement en vue détail), le dropdown, les
           rails ET les détails per-step ("Voir le détail des étapes")
           qui s'affichent mal sur une seule ligne. */
        .transcript-section--inline .downloads-block,
        .transcript-section--inline .pipeline-box,
        .transcript-section--inline .transcript-meta-details,
        .transcript-section--inline .transcript-meta-title,
        .transcript-section--inline .transcript-status-icon,
        .transcript-section--inline .transcript-status-label,
        .transcript-section--inline .status-details { display: none !important; }
        /* En vue détail persistante (data-persistent-summary=1) on
           cache le bandeau statut redondant (la pastille à côté du nom
           technique le porte déjà via tooltip) ET le `<details>` per-step
           dupliqué. La pastille mini reste seul source de vérité visuelle. */
        .transcript-section[data-persistent-summary="1"] .transcript-status-line,
        .transcript-section[data-persistent-summary="1"] .status-details { display: none !important; }
        .transcript-section--inline .transcript-status-line {
            background: transparent !important; border: 0 !important;
            padding: 0 !important; margin: 0 !important;
        }
        .transcript-section--inline .transcript-status-spinner {
            width: 12px; height: 12px;
        }
        .transcript-section--inline .transcript-meta {
            background: transparent; border: 0; padding: 0; margin: 0;
        }
        /* ── Vue DÉTAIL ───────────────────────────────────────────── */
        .file-detail { padding: 0.6rem 0.2rem; }
        .file-detail-header {
            display: flex; justify-content: space-between; align-items: center;
            gap: 0.5rem; margin-bottom: 0.6rem;
        }
        /* Bouton "← Liste" : discret, en lien-texte, pas un gros bouton.
           Hover = soulignement subtil. */
        .file-detail-back {
            background: transparent; border: 0; cursor: pointer;
            color: #64748b; font-size: 0.72rem; line-height: 1;
            padding: 0.15rem 0.25rem; border-radius: 4px;
        }
        .file-detail-back:hover { color: #1d4ed8; text-decoration: underline; }
        .file-detail-back:focus { outline: 2px solid #93c5fd; outline-offset: 1px; }
        .file-detail-title-row {
            display: flex; gap: 0.5rem; align-items: center;
            margin: 0.3rem 0 0.15rem 0;
        }
        .file-detail-title-input {
            flex: 1; min-width: 0; font-size: 1.05rem; font-weight: 600;
            color: #0f172a; padding: 0.35rem 0.55rem;
            border: 1px solid transparent; border-radius: 6px;
            background: transparent; line-height: 1.25;
        }
        .file-detail-title-input:hover,
        .file-detail-title-input:focus {
            border-color: #cbd5e1; background: #fff; outline: none;
        }
        .file-detail-rename-btn { white-space: nowrap; }
        .file-detail-rename-btn:disabled { opacity: 0.4; cursor: default; }
        .file-detail-techline {
            display: flex; align-items: center; justify-content: space-between;
            gap: 0.45rem; margin: 0 0 0.15rem 0.6rem;
            font-size: 0.78rem; color: #64748b;
        }
        .file-detail-fullinfo { margin-top: 0.2rem; }
        /* Mode "page détail" : on cache le titre de l'onglet + le bouton
           purger + la liste des autres sessions. Seul le détail demandé
           est visible, pour vraiment ressembler à une page dédiée. */
        .tab-pane[data-tab="transfers"].detail-active .dsfr-inline-actions,
        .tab-pane[data-tab="transfers"].detail-active #transfer-live { display: none !important; }
        /* En vue détail : on n'affiche pas le bandeau session
           (code + état "Enrôlé") — l'utilisateur a cliqué un fichier
           précis, le contexte session n'apporte rien ici. */
        .session-device-name {
            font-weight: 600; color: #0f172a; font-size: 0.92rem;
            margin-right: 0.35rem;
        }
        .tab-pane[data-tab="transfers"].detail-active .session-item > .session-device-name,
        .tab-pane[data-tab="transfers"].detail-active .session-item > .code,
        .tab-pane[data-tab="transfers"].detail-active .session-item > .status-badge { display: none !important; }
        .tab-pane[data-tab="transfers"].detail-active .session-item {
            border: 0 !important; padding: 0 !important; background: transparent !important;
        }
        /* Résumé persistant en vue détail (non collapsible) */
        .transcript-meta-persistent-title {
            font-weight: 600; color: #0f172a; font-size: 0.78rem;
            margin-bottom: 0.2rem;
        }
        /* ── Responsive mobile ─────────────────────────────────────── */
        @media (max-width: 700px) {
            .file-row-compact {
                grid-template-columns: auto minmax(0,1fr) auto auto;
                grid-template-rows: auto auto;
                gap: 0.25rem 0.5rem;
            }
            .file-row-status-mini { grid-column: 1; grid-row: 1; }
            .file-row-title       { grid-column: 2; grid-row: 1; }
            .file-row-expand      { grid-column: 3; grid-row: 1; }
            .file-row-delete      { grid-column: 4; grid-row: 1; }
            .file-row-meta        { grid-column: 1 / 5; grid-row: 2; }
            .file-row-expanded    { padding-left: 0.7rem; }
            .tabs-nav { font-size: 0.85rem; }
            .tab-btn { padding: 0.45rem 0.55rem; }
            .downloads-block { flex-direction: column; align-items: stretch; }
            .downloads-select { width: 100%; }
        }
        @keyframes transcriptSpin { to { transform: rotate(360deg); } }
        .transcript-status-label { font-weight: 500; }
        .transcript-meta {
            background: #f8fafc; border-left: 3px solid #3b7dd8;
            padding: 0.35rem 0.55rem;
            margin-bottom: 0.3rem;
            border-radius: 4px;
            border-bottom: 1px dashed #cbd5e1;
            padding-bottom: 0.5rem;
        }
        .transcript-meta-title { font-weight: 600; color: #0f172a; font-size: 0.84rem; }
        .transcript-meta-details { margin-top: 0.2rem; font-size: 0.76rem; }
        .transcript-meta-details summary { cursor: pointer; color: #475569; user-select: none; }
        .transcript-meta-details summary:hover { color: #0f172a; }
        .transcript-meta-keypoints { margin: 0.3rem 0 0 0; font-family: inherit; white-space: pre-wrap; font-size: 0.76rem; color: #475569; }
        .transcript-download-block { font-size: 0.76rem; }
        .transcript-download-title { font-size: 0.74rem; color: #64748b; margin-bottom: 0.2rem; }
        .transcript-download-row { display: flex; justify-content: space-between; align-items: center; padding: 0.15rem 0; gap: 0.5rem; flex-wrap: wrap; }
        .transcript-download-label { color: #334155; font-size: 0.78rem; min-width: 0; flex: 1; }
        .transcript-download-buttons { display: flex; gap: 0.25rem; flex-wrap: wrap; }
        .transcript-fmt-btn { display: inline-block; padding: 0.08rem 0.4rem; border: 1px solid #cbd5e1; border-radius: 4px; font-size: 0.7rem; color: #1e293b; background: #fff; text-decoration: none; }
        .transcript-fmt-btn:hover { background: #e0e7ff; border-color: #6366f1; }
        .file-links-row {
            display: flex; align-items: center; justify-content: space-between; gap: 0.6rem;
        }
        .file-links-actions { flex: 1; min-width: 0; }
        .file-links-block { margin-top: 0.35rem; }
        .file-links-title { font-size: 0.74rem; color: #64748b; margin-right: 0.4rem; }
        .impact-icon-btn {
            width: 20px; height: 20px; border-radius: 999px; border: 1px solid #cbd5e1;
            background: #fff; color: #64748b; cursor: pointer; font-size: 12px; font-weight: 700;
            line-height: 1; display: inline-flex; align-items: center; justify-content: center;
            flex: 0 0 auto;
        }
        .impact-icon-btn.loading {
            color: #1d4ed8; border-color: #93c5fd; background: #dbeafe;
        }
        .impact-icon-btn.computed {
            color: #065f46; border-color: #6ee7b7; background: #d1fae5;
        }
        .pipeline-box { margin-top: 0.45rem; }
        .railroad {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 0.25rem;
        }
        .rail-segment {
            flex: 1 1 0;
            display: flex;
            align-items: center;
            min-width: 0;
        }
        .rail-node {
            width: 18px;
            height: 18px;
            border-radius: 999px;
            border: 2px solid #cbd5e1;
            background: #fff;
            color: #64748b;
            font-size: 10px;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            flex: 0 0 auto;
        }
        .rail-line {
            height: 3px;
            flex: 1 1 auto;
            margin: 0 4px;
            border-radius: 999px;
            background: #e2e8f0;
        }
        .rail-line-tail {
            flex: 1 1 auto;
            margin-left: 4px;
            margin-right: 0;
        }
        .rail-segment.done .rail-node {
            border-color: #22c55e;
            background: #dcfce7;
            color: #166534;
        }
        .rail-segment.done .rail-line { background: #86efac; }
        .rail-segment.active .rail-node {
            border-color: #3b82f6;
            background: #dbeafe;
            color: #1d4ed8;
        }
        .rail-segment.blocked .rail-node {
            border-color: #ef4444;
            background: #fee2e2;
            color: #991b1b;
        }
        .rail-labels {
            margin-top: 0.25rem;
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 0.3rem;
            font-size: 0.68rem;
            color: #64748b;
        }
        .rail-labels span { text-align: center; }
        .quality-help {
            cursor: help; color: #64748b; font-size: 0.78rem; margin-left: 0.25rem;
            border: 1px solid #cbd5e1; border-radius: 999px; padding: 0 0.35rem;
            background: #fff;
        }
        /* Tabs navigation (3-4 onglets en haut de page) */
        .tabs-nav {
            display: flex; gap: 0.4rem; margin-bottom: 1rem;
            border-bottom: 1px solid #cbd5e1; padding-bottom: 0;
            overflow-x: auto;
        }
        .tab-btn {
            border: 0; background: transparent; cursor: pointer;
            padding: 0.55rem 0.9rem; font-size: 0.92rem; font-weight: 500;
            color: #475569; border-bottom: 3px solid transparent;
            margin-bottom: -1px; white-space: nowrap;
        }
        .tab-btn:hover { color: #0f172a; }
        .tab-btn[aria-selected="true"] {
            color: #0f172a; font-weight: 700;
            border-bottom-color: #1d4ed8;
        }
        /* Par défaut on cache tous les panneaux ; JS active le bon onglet
           dès que loadDevices a déterminé l'état. */
        .tab-pane { display: none; }
        .tab-pane.is-active { display: block; }
        /* Toasts feedback (bas-droite, disparaît après 4s) */
        .toast {
            position: fixed; right: 1rem; bottom: 1rem; z-index: 9999;
            max-width: 360px; padding: 0.6rem 0.8rem; border-radius: 8px;
            font-size: 0.85rem; line-height: 1.3; color: #fff;
            background: #1e293b; box-shadow: 0 4px 14px rgba(0,0,0,0.18);
            opacity: 0; transform: translateY(8px); transition: opacity .25s, transform .25s;
        }
        .toast.toast-show { opacity: 1; transform: translateY(0); }
        .toast-error { background: #b91c1c; }
        .toast-success { background: #047857; }
        .security-notice {
            margin: 0.75rem 0 1rem;
            padding: 0.7rem 0.8rem;
            border: 1px solid #fcd34d;
            background: #fffbeb;
            color: #92400e;
            border-radius: 8px;
            font-size: 0.88rem;
            line-height: 1.35;
        }
        .transfer-live {
            margin: 0.25rem 0 0.8rem;
            padding: 0.6rem 0.7rem;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            background: #f8fafc;
            font-size: 0.84rem;
        }
        .transfer-live-title {
            font-weight: 700;
            color: #0f172a;
            margin-bottom: 0.35rem;
        }
        .transfer-live-list {
            max-height: 160px;
            overflow-y: auto;
            display: grid;
            gap: 0.3rem;
        }
        .transfer-live-row {
            display: flex; flex-direction: column; gap: 0.2rem;
            padding: 0.4rem 0.55rem; border: 1px solid #e2e8f0;
            border-radius: 6px; background: #ffffff; color: #334155;
        }
        .transfer-live-line1 {
            display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem;
            font-size: 0.82rem;
        }
        .transfer-live-msg { color: #64748b; font-size: 0.78rem; }
        .railroad-mini .rail-node { width: 14px; height: 14px; font-size: 8px; }
        .railroad-mini .rail-line { height: 2px; }
        .transfer-live-code {
            color: #64748b;
            font-family: monospace;
            font-size: 0.8rem;
        }
        .transfer-live-name {
            max-width: 210px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .transfer-live-empty {
            color: #64748b;
        }
        .activity-inline {
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
            margin-bottom: 0.5rem;
        }
        .activity-description {
            margin: 0;
            font-size: 0.8rem;
            color: #64748b;
            line-height: 1.35;
        }
        .activity-main {
            min-width: 0;
        }
        .activity-spinner {
            width: 14px;
            height: 14px;
            border: 2px solid #cbd5e1;
            border-top-color: #cbd5e1;
            border-radius: 999px;
            flex: 0 0 auto;
            opacity: 0.35;
        }
        .activity-spinner.active {
            opacity: 1;
            border-top-color: #2563eb;
            animation: activity-spin 0.9s linear infinite;
        }
        @keyframes activity-spin {
            to { transform: rotate(360deg); }
        }
        .activity-mini {
            min-width: 0;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 0.3rem;
        }
        .activity-mini-text {
            font-size: 0.8rem;
            color: #475569;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .activity-rail {
            display: flex;
            align-items: center;
            gap: 0.18rem;
            width: 100%;
        }
        .activity-meta-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.6rem;
            margin-top: 0.15rem;
        }
        .activity-status-inline {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            min-width: 0;
            flex: 1;
        }
        .activity-dot {
            width: 14px;
            height: 14px;
            border-radius: 999px;
            background: #e5e7eb;
            color: #475569;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 0.62rem;
            font-weight: 700;
            flex: 0 0 auto;
        }
        .activity-dot.active {
            background: #2563eb;
            color: #fff;
        }
        .activity-link {
            width: 100%;
            height: 2px;
            border-radius: 999px;
            background: #e5e7eb;
        }
        .activity-link.active {
            background: #93c5fd;
        }
        .activity-toggle-link {
            font-size: 0.78rem;
            color: #64748b;
            text-decoration: none;
            border-bottom: 1px dotted #cbd5e1;
            width: fit-content;
            flex: 0 0 auto;
        }
        .activity-toggle-link:hover {
            color: #334155;
            border-bottom-color: #94a3b8;
        }
        .recent-activities-panel { display: block; }
        .dsfr-inline-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 0.6rem;
        }
        .dsfr-inline-actions .fr-btn { width: auto; }
        .fr-header__service-tagline {
            max-width: 48rem;
        }
        .beta-badge {
            display: inline-block;
            margin-left: 0.45rem;
            padding: 0.08rem 0.35rem;
            border-radius: 0.35rem;
            background: #e1000f;
            color: #fff;
            font-size: 0.66rem;
            font-weight: 700;
            line-height: 1.1;
            transform: rotate(-12deg);
            transform-origin: center;
            vertical-align: top;
        }
        .fr-header__body-row {
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
        }
    </style>
</head>
<body>
<header role="banner" class="fr-header">
  <div class="fr-header__body">
    <div class="fr-container">
      <div class="fr-header__body-row">
        <div class="fr-header__brand fr-enlarge-link">
          <div class="fr-header__brand-top">
            <div class="fr-header__logo">
              <p class="fr-logo">
                République
                <br>Française
              </p>
            </div>
          </div>
          <div class="fr-header__service">
            <a href="#" title="Accueil MIrAI">
              <p class="fr-header__service-title">MIrAI - Mes réunions IA <span class="beta-badge">Bêta</span></p>
            </a>
            <p class="fr-header__service-tagline">Enregistrez depuis votre mobile, laissez l'IA transcrire et synthétiser vos réunions et notes vocales</p>
          </div>
        </div>
        <div class="header-user">
          <span class="header-user-name">{{ user.name or user.email }}</span>
          <a class="fr-link" href="/logout">Déconnexion</a>
          <button type="button" id="advanced-toggle" class="advanced-toggle"
                  onclick="toggleAdvancedDl(this)"
                  title="Mode avancé — affiche tous les téléchargements à plat (sans le menu Autres).&#10;Astuce power-user : maintiens Alt pour un peek temporaire."
                  aria-pressed="false">Mode avancé</button>
        </div>
      </div>
    </div>
  </div>
</header>

<main class="fr-container page-shell">
<div class="container">

    <!-- Navigation onglets — ordre : (1) Mes transferts (vue principale,
         là où l'utilisateur passe le plus de temps), (2) Mes appareils,
         (3) Enrôler un nouvel appareil. L'onglet par défaut est choisi
         par loadDevices : si aucun device enrôlé → "Enrôler", sinon
         "Mes transferts et analyses". -->
    <nav class="tabs-nav" role="tablist" aria-label="Sections principales">
        <button type="button" class="tab-btn" role="tab" data-tab="transfers" id="tab-btn-transfers">
            Mes réunions (IA)
        </button>
        <button type="button" class="tab-btn" role="tab" data-tab="devices" id="tab-btn-devices">
            Mes appareils
        </button>
        <button type="button" class="tab-btn" role="tab" data-tab="generate" id="tab-btn-generate">
            Enrôler un nouvel appareil
        </button>
        <button type="button" class="tab-btn" role="tab" data-tab="trash" id="tab-btn-trash">
            Corbeille
        </button>
    </nav>

    <div class="card tab-pane" data-tab="trash">
        <div class="dsfr-inline-actions">
            <h1 style="font-size:1.1rem;">Corbeille</h1>
            <span style="color:#64748b;font-size:0.78rem;">
                Les éléments sont automatiquement supprimés après <strong>30 jours</strong>.
            </span>
        </div>
        <div id="trash-list" style="margin-top:0.6rem;">
            <p style="color:#999;font-size:0.85rem;">Chargement de la corbeille...</p>
        </div>
    </div>

    <div class="card tab-pane" data-tab="generate" id="enrollment-card">
        <h1>Téléverser facilement vos fichiers audio depuis votre téléphone</h1>
        <p class="subtitle">Enrôler votre mobile pour permettre un upload facilité et sécurisé de votre enregistrement</p>

        <!-- Hidden by default; revealed by loadDevices() if no active device is enrolled.
             When at least one active device exists, we show #enrollment-collapsed
             instead so the user isn't presented with a form they don't need. -->
        <div id="enrollment-collapsed" style="display:none;padding:0.45rem 0;">
            <p style="margin:0;font-size:0.88rem;color:#475569;">
                Vous avez déjà au moins un appareil enrôlé. Vous pouvez uploader directement depuis lui.
            </p>
            <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary" style="margin-top:0.5rem;"
                    onclick="showEnrollmentForm()">Enrôler un nouvel appareil</button>
        </div>

        <div id="generate-form">
            <div class="form-group fr-select-group">
                <label class="fr-label" for="ttl">Fenêtre d'enrôlement</label>
                <p style="margin:0.2rem 0 0.4rem 0;color:#64748b;font-size:0.78rem;">
                    Délai pendant lequel le QR peut être scanné par un mobile pour s'enrôler.
                    Une fois enrôlé, l'appareil est valide 15 jours pour uploader.
                </p>
                <select id="ttl" class="fr-select">
                    {% if short_ttl_enabled %}
                    <option value="15s">15 secondes (test)</option>
                    <option value="30s">30 secondes (test)</option>
                    {% endif %}
                    <option value="5" selected>5 minutes (recommandé)</option>
                    <option value="15">15 minutes</option>
                    <option value="30">30 minutes</option>
                    <option value="60">1 heure</option>
                </select>
            </div>
            <!-- Le quota max-uploads/token n'est plus exposé à l'utilisateur (cf. mydevices UX
                 simplification). Reste en hidden input pour que le JS continue de lire la
                 valeur sans casser le flow. Default 299 = MAX_UPLOADS_PER_SESSION côté
                 serveur (le serveur clampe via min() de toute façon). -->
            <input type="hidden" id="max-uploads" value="299">
            <div class="form-group fr-checkbox-group">
                <input type="checkbox" id="auto-transcribe" checked>
                <label class="fr-label" for="auto-transcribe">
                    Lancer la retranscription automatique et l'ajouter dans MirAI Compte-rendu
                </label>
                <p style="margin-top:0.2rem;color:#64748b;font-size:0.78rem;">
                    Dans tous les cas, les fichiers d'enregistrement seront optimisés pour la voix.
                </p>
            </div>
            <button class="btn-primary fr-btn fr-btn--sm" id="btn-generate" onclick="generateCode()">
                Générer un code
            </button>
        </div>

        <div class="fr-alert fr-alert--warning fr-mt-2w">
            <p>Pour limiter les risques en cas de perte ou vol de votre téléphone. Supprimez régulièrement les fichiers du téléphone, par exemple après la retranscription.</p>
        </div>

        <div class="result" id="result">
            <p style="font-size:0.9rem; color:#666; margin-bottom:0.5rem;">
                Code à saisir sur le mobile :
            </p>
            <div class="simple-code" id="display-code"></div>
            <div class="qr-container">
                <img id="qr-img" width="200" height="200" alt="QR Code">
            </div>
            <p class="expires" id="display-expires"></p>
            <p class="expires" id="display-remaining"></p>
            <button class="btn-primary fr-btn fr-btn--secondary fr-btn--sm" style="margin-top:1rem;"
                    onclick="resetForm()">Générer un nouveau code</button>
        </div>
    </div>

    <div class="card tab-pane" data-tab="devices">
        <div class="dsfr-inline-actions">
            <h1 style="font-size:1.05rem;">Appareils enrôlés</h1>
            <div style="display:flex;align-items:center;gap:0.35rem;">
                <button id="device-filter-btn" class="btn-primary fr-btn fr-btn--sm fr-btn--secondary device-filter-btn"
                        onclick="toggleDeviceScope()">Voir révoqués</button>
                <button class="btn-primary btn-danger-mini fr-btn fr-btn--sm fr-btn--tertiary-no-outline" id="revoke-all-devices-btn" onclick="revokeAllDevices()">Révoquer tous</button>
            </div>
        </div>
        <p class="subtitle" style="margin-top:0.4rem;margin-bottom:0.8rem;">
            Ces appareils peuvent uploader sans rescanner tant que leur enrôlement est valide.
        </p>
        <div id="devices-list" style="font-size:0.84rem;color:#64748b;">Chargement appareils...</div>
    </div>

    <div class="card tab-pane" data-tab="transfers">
        <div id="recent-activities-panel" class="recent-activities-panel open">
            <div class="dsfr-inline-actions">
                <h1 style="font-size:1.1rem;">Mes réunions (IA)</h1>
                <!-- Action lourde « tout mettre à la corbeille ». Cachée par
                     défaut, visible uniquement en mode avancé (toggle ON ou
                     Alt enfoncé) via .advanced-only + body.adv-mode. Icône
                     composée : benne à ordures + 2 silhouettes d'éboueurs +
                     mini-poubelle qui bascule au hover (animation CSS). -->
                <button type="button" id="purge-btn" class="icon-btn icon-btn-purge advanced-only" disabled
                        onclick="purgeSessions()"
                        title="Mettre toute la liste à la corbeille (purgée définitivement après 30 jours)"
                        aria-label="Tout mettre à la corbeille">
                    <svg viewBox="0 0 32 24" fill="none" stroke="currentColor"
                         stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"
                         aria-hidden="true" class="purge-icon-svg">
                        <!-- mini-poubelle prête à être chargée (anime translateY au hover) -->
                        <g class="purge-bin">
                          <rect x="2" y="14" width="4" height="6" rx="0.4"/>
                          <line x1="1.5" y1="14" x2="6.5" y2="14"/>
                          <line x1="3" y1="13" x2="5" y2="13"/>
                        </g>
                        <!-- benne du camion + cabine -->
                        <path d="M8 8h13v12H8z" fill="currentColor" fill-opacity="0.08"/>
                        <path d="M8 8h13v12H8z"/>
                        <path d="M21 12h4l3 3v5h-7z" fill="currentColor" fill-opacity="0.05"/>
                        <path d="M21 12h4l3 3v5h-7z"/>
                        <path d="M22 14h3" />
                        <!-- 4 stries verticales sur la benne -->
                        <path d="M11 9v10M14 9v10M17 9v10"/>
                        <!-- roues -->
                        <circle cx="12" cy="21" r="1.6" fill="currentColor" fill-opacity="0.15"/>
                        <circle cx="12" cy="21" r="1.6"/>
                        <circle cx="24" cy="21" r="1.6" fill="currentColor" fill-opacity="0.15"/>
                        <circle cx="24" cy="21" r="1.6"/>
                        <!-- 2 silhouettes d'éboueurs (têtes + corps simplifiés) à l'arrière du camion -->
                        <g class="purge-workers">
                          <!-- éboueur 1 -->
                          <circle cx="29" cy="10" r="1.1" fill="currentColor" fill-opacity="0.2"/>
                          <path d="M28.3 11.2v3.2M29.6 11.2v3.2"/>
                          <!-- éboueur 2 -->
                          <circle cx="30.5" cy="11.5" r="1" fill="currentColor" fill-opacity="0.2"/>
                          <path d="M29.9 12.5v2.8M31.1 12.5v2.8"/>
                        </g>
                    </svg>
                </button>
            </div>
            <!-- "Transferts en cours" : un bloc par fichier in-flight avec
                 son propre chemin de fer + statut transcription inline.
                 Disparaît quand 0 transfert en cours. -->
            <div class="transfer-live" id="transfer-live" style="display:none;"></div>
            <div class="sessions-list" id="sessions-list">
                <p style="color:#999; font-size:0.85rem;">Chargement...</p>
            </div>
        </div>
    </div>
</div>
</main>

<script>
const impactCache = {};
const impactLoading = new Set();
let showAllDevices = false;
// Map qr_token → {device_name, status, retention_expires_at} populée par
// loadDevices. Sert à enrichir l'en-tête de chaque session dans la liste
// des transferts (montre "iPhone CODE (active)" au lieu de juste "CODE").
const _devicesByQrToken = {};
// État vue transferts : null = liste compacte, fileId = vue détail pour
// ce fichier. Switch via showFileDetail / showFilesList.
let _detailFileId = null;
function showFileDetail(fileId) {
    _detailFileId = fileId;
    activateTab('transfers');
    // Active le mode "page détail" : masque le titre + bouton purge +
    // bordure carte pour donner l'illusion d'une vraie page dédiée.
    const pane = document.querySelector('.tab-pane[data-tab="transfers"]');
    if (pane) pane.classList.add('detail-active');
    loadSessions({ force: true });
    requestAnimationFrame(() => window.scrollTo(0, 0));
}
function showFilesList() {
    _detailFileId = null;
    const pane = document.querySelector('.tab-pane[data-tab="transfers"]');
    if (pane) pane.classList.remove('detail-active');
    loadSessions({ force: true });
}
// Toggle l'affichage de la zone résumé sous une ligne compacte. Le
// chevron tourne (CSS) selon la classe is-open.
// ── Info-bulle file d'attente Kevent (vue détail) ───────────────────────
// Poll 10s vers /api/queue-status. Format texte conformément à la spec.
const _TERMINAL_TS = new Set([
    'completed','kevent_completed','failed','kevent_failed',
    'kevent_partially_completed','mcr_pushed','mcr_auth_failed',
    'mcr_rejected','mcr_push_failed','disabled',
]);
let _queueHintTimer = null;
function _fmtEta(s) {
    if (s == null) return '';
    if (s < 15) return 'quasi immédiat';
    if (s < 60) return `${s} s`;
    const m = Math.floor(s / 60);
    const sec = Math.round((s % 60) / 10) * 10;
    return sec === 0 ? `${m} min` : `${m} min ${String(sec).padStart(2,'0')} s`;
}
// Calcule le texte du hint à partir du payload /api/queue-status.
// Renvoie '' si pas d'info exploitable.
function _formatQueueHint(d) {
    if (!d || d.pending_total == null) return '';
    let txt = '';
    if (d.your_position === 1) {
        txt = '⏳ En tête de file';
    } else if (d.your_position && d.pending_total > 0) {
        const eta = d.eta_seconds;
        const part = eta != null && eta < 15
            ? ' — quasi immédiat'
            : (eta != null ? ` — env. ${_fmtEta(eta)} d'attente` : '');
        txt = `⏳ Position ${d.your_position}/${d.pending_total} dans la file${part}`;
    } else if (d.pending_total > 0) {
        txt = `⏳ ${d.pending_total} job${d.pending_total > 1 ? 's' : ''} en attente`;
    } else if (d.processing_total > 0) {
        txt = '⏳ Tour suivant';   // file vide mais quelqu'un est en train de tourner devant
    } else {
        txt = '⏳ Réservation de la file…';  // file complètement vide, transitoire (entre 2 jobs)
    }
    if (txt && d.stale) txt += ' (estimation)';
    return txt;
}

// Poll global : scanne UNIQUEMENT les widgets queue-hint marqués comme
// pollables (data-pollable="1") — c'est-à-dire ceux dont le fichier
// associé est encore dans un statut polling (kevent_queued/transcribing/
// processing). Les widgets sur des fichiers terminaux (kevent_completed,
// kevent_failed, kevent_partially_completed) restent dans le DOM mais
// sans le flag pollable → on ne les met PAS à jour, on les vide même
// si le fichier vient juste de transiter vers un état terminal.
async function _pollQueueHintAll() {
    const widgets = document.querySelectorAll('[data-queue-hint-for][data-pollable="1"]');
    // Vide les widgets qui ne sont plus pollables (transition kevent_*ing →
    // kevent_completed/failed/partially) — sinon le dernier texte du poll
    // précédent reste affiché de façon trompeuse ("Réservation de la file…"
    // sur un fichier terminal).
    document.querySelectorAll('[data-queue-hint-for]:not([data-pollable="1"])').forEach((el) => {
        if (el.textContent) el.textContent = '';
    });
    if (widgets.length === 0) return;

    // Regroupe par job_id (null = générique).
    const byJobId = new Map();
    widgets.forEach((el) => {
        const jid = (el.getAttribute('data-queue-job-id') || '').trim() || null;
        if (!byJobId.has(jid)) byJobId.set(jid, []);
        byJobId.get(jid).push(el);
    });

    await Promise.all(Array.from(byJobId.entries()).map(async ([jid, els]) => {
        const url = jid
            ? `/api/queue-status?service_type=audio&job_id=${encodeURIComponent(jid)}`
            : '/api/queue-status?service_type=audio';
        try {
            const r = await fetch(url, { cache: 'no-store' });
            const d = await r.json();
            const txt = _formatQueueHint(d);
            els.forEach((el) => {
                el.textContent = txt;
                el.classList.toggle('queue-hint-stale', !!(d && d.stale));
            });
        } catch (e) { /* silencieux — on retentera dans 10s */ }
    }));
}

// Activé tant qu'au moins un widget queue-hint est dans le DOM. Démarré
// idempotemment à chaque render (loadSessions) ; auto-stop dans le poll quand
// il n'y a plus de widget (sortie de liste vers vue Mes appareils, etc.).
function ensureQueueHintPolling() {
    if (_queueHintTimer) return;          // déjà actif
    _pollQueueHintAll();                  // 1er appel immédiat
    _queueHintTimer = setInterval(() => {
        // Auto-stop : plus AUCUN widget pollable dans le DOM.
        if (document.querySelectorAll('[data-queue-hint-for][data-pollable="1"]').length === 0) {
            // Un dernier passage pour vider les widgets non-pollables qui
            // auraient encore du texte résiduel.
            _pollQueueHintAll();
            clearInterval(_queueHintTimer);
            _queueHintTimer = null;
            return;
        }
        _pollQueueHintAll();
    }, 10000);
}

// Aliases pour compat ascendante (anciens call-sites avant la généralisation).
function startQueueHintDetail(fileId) { ensureQueueHintPolling(); }
function stopQueueHintDetail() {
    if (_queueHintTimer) clearInterval(_queueHintTimer);
    _queueHintTimer = null;
}

// Affiche un modal avec toutes les infos techniques du fichier :
// statut + engine + langue + étapes IA (✓/✗ avec description) + impact LUFS.
async function openFileInfoModal(fileId) {
    const cached = (window._fileInfoCache || {})[fileId];
    if (!cached) {
        showToast('Données techniques en cours de chargement.', 'error');
        return;
    }
    const STEPS = {
        'transcript':              { label: 'Transcription brute',          desc: 'Texte issu de Whisper (faster-whisper).' },
        'transcript-tagged':       { label: 'Identification des locuteurs', desc: 'Diarisation pyannote — sépare le texte par interlocuteur. Peut échouer sur monolocuteur/audio très court.' },
        'transcript-corrected':    { label: 'Correction des sigles',        desc: 'LLM relit avec un glossaire métier pour corriger les acronymes.' },
        'transcript-cleaned':      { label: 'Nettoyage hors-sujet',         desc: 'LLM retire les passages parasites (faux départs, bruits verbalisés).' },
        'transcript-reformulated': { label: 'Discours indirect',            desc: 'LLM reformule au style indirect pour lecture rapide.' },
        'meeting-cr':              { label: 'Compte-rendu structuré',      desc: 'LLM produit l\\'analyse 5 sections : acteurs, thématiques, décisions, gaps, recommandations.' },
    };
    // État par étape :
    //   ok    → output présent (vert ✓)
    //   fail  → output absent ET statut global = failed (rouge ✗ + cause)
    //   run   → output absent ET pipeline en cours, première étape pending
    //   wait  → output absent, pas encore tentée
    const status = cached.status || '';
    const isFail = (window._FAILED_TS && window._FAILED_TS.has(status))
        || ['failed','kevent_failed','mcr_auth_failed','mcr_rejected','mcr_push_failed'].includes(status);
    const isRunning = ['pending','processing','kevent_queued','kevent_transcribing','kevent_processing'].includes(status);
    let runMarked = false;
    const stepsHtml = Object.keys(STEPS).map(k => {
        const ok = !!(cached.outputs || {})[k];
        let icon, color, suffix = '';
        if (ok) { icon = '✓'; color = '#10b981'; }
        else if (isFail) {
            icon = '✗'; color = '#b91c1c';
            suffix = ` <small style="color:#94a3b8">(échec du pipeline — étape non aboutie)</small>`;
        } else if (isRunning && !runMarked) {
            icon = '⏳'; color = '#2563eb'; runMarked = true;
            suffix = ` <small style="color:#94a3b8">(en cours)</small>`;
        } else if (isRunning) {
            icon = '☐'; color = '#94a3b8';
            suffix = ` <small style="color:#94a3b8">(en attente)</small>`;
        } else {
            icon = '☐'; color = '#94a3b8';
        }
        return `<div class="modal-step">
            <span style="color:${color};font-weight:700;font-size:1rem;">${icon}</span>
            <div>
                <div class="modal-step-label">${escapeHtml(STEPS[k].label)}${suffix}</div>
                <div class="modal-step-desc">${escapeHtml(STEPS[k].desc)}</div>
            </div>
        </div>`;
    }).join('');

    // Récupère ou crée le modal
    let modal = document.getElementById('file-info-modal');
    if (!modal) {
        modal = document.createElement('dialog');
        modal.id = 'file-info-modal';
        modal.className = 'file-info-modal';
        document.body.appendChild(modal);
        modal.addEventListener('click', (e) => {
            // Click sur backdrop ferme le modal
            if (e.target === modal) modal.close();
        });
    }
    modal.innerHTML = `
        <div class="modal-header">
            <h3>Détails techniques</h3>
            <button class="modal-close" onclick="document.getElementById('file-info-modal').close()" aria-label="Fermer">✕</button>
        </div>
        <div class="modal-body">
            <div class="modal-section">
                <div class="modal-section-title">Pipeline IA</div>
                <div class="modal-status">
                    <strong>Statut :</strong> ${escapeHtml(cached.label)}
                    <small style="color:#94a3b8;">(${escapeHtml(cached.status)}${cached.engine ? ' · ' + escapeHtml(cached.engine) : ''})</small>
                </div>
                ${cached.language ? `<div><strong>Langue détectée :</strong> ${escapeHtml(cached.language)}</div>` : ''}
            </div>
            <div class="modal-section">
                <div class="modal-section-title">Étapes</div>
                <div class="modal-steps">${stepsHtml}</div>
            </div>
            <div class="modal-section">
                <div class="modal-section-title">Normalisation audio (LUFS)</div>
                <p class="modal-explain">La normalisation aligne le niveau sonore sur la cible −16 LUFS (compatible voix). Le calcul mesure les LUFS / TP / LRA avant et après transcodage.</p>
                <button class="fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="loadNormalizationImpact('${fileId}')">Calculer l'impact</button>
                <div class="modal-impact-result" id="modal-impact-${fileId}">
                    ${(impactCache[fileId] && impactCache[fileId].text) ? escapeHtml(impactCache[fileId].text) : '<small style="color:#94a3b8;">Pas encore calculé.</small>'}
                </div>
            </div>
        </div>
    `;
    if (typeof modal.showModal === 'function') {
        modal.showModal();
    } else {
        modal.setAttribute('open', '');
    }
}

// Renomme le titre suggéré (suggested_filename) du fichier en vue détail.
// Persiste via POST /api/file/<id>/rename → token-issuer interne.
async function renameDetailTitle(fileId, btn) {
    const input = document.querySelector(`[data-detail-title-for="${fileId}"]`);
    if (!input) return;
    const newTitle = (input.value || '').trim();
    if (!newTitle) {
        showToast('Le titre ne peut pas être vide.', 'error');
        return;
    }
    btn.disabled = true;
    try {
        const resp = await fetch(`/api/file/${fileId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'rename_failed');
        input.dataset.originalTitle = newTitle;
        showToast('Titre renommé.', 'success');
        // Force un refresh des sessions pour propager le nouveau titre.
        loadSessions({ force: true });
    } catch (e) {
        btn.disabled = false;
        showToast(`Renommage échoué : ${e.message}`, 'error');
    }
}
// Active le bouton "Renommer" quand le titre est modifié (vs valeur initiale).
document.addEventListener('input', (ev) => {
    const t = ev.target;
    if (!t || !t.matches('.file-detail-title-input')) return;
    const original = t.dataset.originalTitle || '';
    const current = (t.value || '').trim();
    const btn = t.parentElement && t.parentElement.querySelector('.file-detail-rename-btn');
    if (btn) btn.disabled = !current || current === original;
});

function toggleRowExpand(btn) {
    const wrapper = btn.closest('.file-row-compact-wrapper');
    if (!wrapper) return;
    const exp = wrapper.querySelector('.file-row-expanded');
    if (!exp) return;
    const open = exp.style.display !== 'none';
    exp.style.display = open ? 'none' : '';
    btn.classList.toggle('is-open', !open);
    const lbl = btn.querySelector('.file-row-expand-label');
    if (lbl) lbl.textContent = open ? 'détails' : 'replier';
    btn.setAttribute('aria-label', open ? 'Voir le résumé' : 'Masquer le résumé');
}
// Format helpers pour la vue liste compacte.
function _formatDateCompact(iso) {
    if (!iso) return '';
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        return d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit', year: '2-digit' })
            + ' ' + d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
    } catch (e) { return ''; }
}
function _formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return '';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return m > 0 ? `${m}m${String(s).padStart(2,'0')}s` : `${s}s`;
}
// Durée de rétention device en jours (lue depuis DEVICE_TOKEN_RETENTION_HOURS
// côté serveur — 15j en prod-bêta, 7j par défaut). Sert aux messages de
// confirmation pour refléter la vraie durée que Renouveler applique.
const deviceRetentionDays = {{ device_retention_days }};

// Mode "Mode avancé" pour les téléchargements : OFF (défaut) montre
// le CR + audio interne + Transcription nettoyée + Discours indirect, le
// reste va dans le menu "Autres". ON affiche tout à plat (pas de menu).
// Persistant en sessionStorage. Astuce power-user non documentée : Alt
// active un peek temporaire (sans changer l'état persistant) — pratique
// pour jeter un œil sans toggler.
let _dlAdvancedMode = false;
try { _dlAdvancedMode = sessionStorage.getItem('mydevices-dl-mode') === 'advanced'; } catch(e){}
let _altPeek = false;

function effectiveAdvancedDl() { return _dlAdvancedMode || _altPeek; }

function updateAdvancedToggleUi() {
    const btn = document.getElementById('advanced-toggle');
    if (btn) {
        btn.classList.toggle('is-on', _dlAdvancedMode);
        btn.classList.toggle('is-peek', _altPeek);
        btn.setAttribute('aria-pressed', _dlAdvancedMode ? 'true' : 'false');
    }
    // body.adv-mode pilote la visibilité de .advanced-only (camion poubelle, etc.).
    document.body.classList.toggle('adv-mode', effectiveAdvancedDl());
}

function toggleAdvancedDl() {
    _dlAdvancedMode = !_dlAdvancedMode;
    try { sessionStorage.setItem('mydevices-dl-mode', _dlAdvancedMode ? 'advanced' : 'simple'); } catch(e){}
    updateAdvancedToggleUi();
    refreshDownloadsBlocks();
}

function refreshDownloadsBlocks() {
    document.querySelectorAll('.transcript-section[data-persistent-summary="1"]').forEach((container) => {
        const fileId = container.getAttribute('data-transcript-file-id');
        if (fileId) loadTranscriptStatus(fileId, container);
    });
}

// Peek temporaire via Alt enfoncé. Modifier-only keydown ne se répète
// pas (autorepeat ignore Alt sur la plupart des navigateurs), donc on
// fire bien une seule fois à l'appui.
document.addEventListener('keydown', (e) => {
    if (e.key === 'Alt' && !_altPeek) {
        _altPeek = true;
        updateAdvancedToggleUi();
        refreshDownloadsBlocks();
        e.preventDefault();
    }
});
document.addEventListener('keyup', (e) => {
    if (e.key === 'Alt' && _altPeek) {
        _altPeek = false;
        updateAdvancedToggleUi();
        refreshDownloadsBlocks();
    }
});
// Si la fenêtre perd le focus pendant un peek (Cmd+Tab…), on annule
// pour ne pas rester coincé en mode peek.
window.addEventListener('blur', () => {
    if (_altPeek) { _altPeek = false; updateAdvancedToggleUi(); refreshDownloadsBlocks(); }
});

// Icônes SVG inline (Heroicons-like, simplifiés). Centralisées ici pour
// que tous les boutons-icône partagent le même rendu et qu'on puisse les
// faire évoluer en un seul endroit.
const ICONS = {
    // Poubelle simple — suppression d'un élément
    trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>',
    // Camion poubelle — purge massive (suppression de toute la liste)
    truck: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 17h2V7a1 1 0 0 1 1-1h9v11h2"/><path d="M14 10h4l3 4v3h-2"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/><path d="M7 10v3M9 10v3M11 10v3"/></svg>',
    // Télécharger un audio : icône "fichier audio" type Finder — document
    // avec coin replié + note de musique à l'intérieur.
    fmt_audio:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#eff6ff"/><path d="M14 2v6h6" fill="#dbeafe"/><path d="M11 12v5.5" stroke="#1d4ed8"/><path d="M11 12l4-1v5.5" stroke="#1d4ed8"/><ellipse cx="10" cy="17.5" rx="1.4" ry="1.1" fill="#1d4ed8" stroke="#1d4ed8"/><ellipse cx="14" cy="16.5" rx="1.4" ry="1.1" fill="#1d4ed8" stroke="#1d4ed8"/></svg>',
    // Écouter : triangle play dans un cercle (style bouton lecteur).
    fmt_play:   '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.12"/><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8" fill="none"/><path d="M10 8.5v7l6-3.5z" fill="currentColor"/></svg>',
    fmt_txt:    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M8 13h8M8 17h8M8 9h2"/></svg>',
    fmt_md:     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 15V9l2.5 3L12 9v6"/><path d="M16 9v6m0 0l-1.5-1.5M16 15l1.5-1.5"/></svg>',
    fmt_docx:   '<svg viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#dbeafe"/><path d="M14 2v6h6" fill="#bfdbfe"/><text x="12" y="18" font-size="6" font-weight="700" fill="#1e40af" text-anchor="middle" font-family="Arial,sans-serif">W</text></svg>',
    fmt_odt:    '<svg viewBox="0 0 24 24" fill="none" stroke="#16a34a" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" fill="#dcfce7"/><path d="M14 2v6h6" fill="#bbf7d0"/><text x="12" y="18" font-size="5" font-weight="700" fill="#166534" text-anchor="middle" font-family="Arial,sans-serif">ODT</text></svg>',
    fmt_json:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 4c-2 0-3 1-3 3v3c0 2-2 2-2 2s2 0 2 2v3c0 2 1 3 3 3"/><path d="M16 4c2 0 3 1 3 3v3c0 2 2 2 2 2s-2 0-2 2v3c0 2-1 3-3 3"/></svg>',
};
function escapeHtml(v) {
    return (v || '').toString().replace(/[&<>"']/g, (s) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    })[s]);
}

function statusLabel(status) {
    const labels = {
        pending: 'En attente',
        scanning: 'Analyse antivirus',
        scan_clean: 'Scan OK',
        scan_infected: 'Infecté',
        transcoding: 'Transcodage',
        transcoded: 'Transcodé',
        ready_for_transfer: 'Prêt transfert',
        transferring: 'Transfert',
        transferred: 'Transféré',
        quarantined: 'Quarantaine',
        transcode_failed: 'Transcodage échoué',
        error: 'Erreur',
    };
    return labels[status] || status;
}

// Phases d'upload où le pipeline tourne encore (avant que la transcription
// ne prenne le relais). Utilisé pour animer le dot dès le départ.
const UPLOAD_IN_PROGRESS_STATES = new Set([
    'pending', 'scanning', 'scan_clean',
    'transcoding', 'ready_for_transfer', 'transferring',
]);
function _uploadStateLabel(status) {
    if (status === 'transferred') return 'Fichier reçu. Transcription en attente.';
    if (UPLOAD_IN_PROGRESS_STATES.has(status)) {
        return `Étape en cours : ${statusLabel(status)}`;
    }
    return statusLabel(status);
}

function tokenValidityDaysLabel(retentionExpiresAt) {
    if (!retentionExpiresAt) return '-';
    const endMs = new Date(retentionExpiresAt).getTime();
    if (!Number.isFinite(endMs)) return '-';
    const diffMs = endMs - Date.now();
    if (diffMs <= 0) return 'expiré';
    const days = diffMs / (24 * 60 * 60 * 1000);
    if (days < 1) return '< 1 jour';
    return `${Math.ceil(days)} jour(s)`;
}

function formatDateTimeShort(isoValue) {
    if (!isoValue) return '-';
    const d = new Date(isoValue);
    if (!Number.isFinite(d.getTime())) return '-';
    return d.toLocaleString('fr-FR', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function tokenIdShort(tokenValue) {
    const raw = (tokenValue || '').toString().trim();
    if (!raw) return '-';
    if (raw.length <= 12) return raw;
    return `${raw.slice(0, 6)}...${raw.slice(-4)}`;
}

function deviceTokenStateLabel(device) {
    const rawStatus = (device && device.status ? String(device.status) : '').toLowerCase();
    if (rawStatus === 'revoked') return 'révoqué';
    if (rawStatus === 'pending') return 'initialisation…';
    const endMs = new Date((device && (device.retention_expires_at || device.session_expires_at)) || '').getTime();
    if (Number.isFinite(endMs) && endMs <= Date.now()) return 'expiré';
    return 'active';
}

function deviceTokenStateColor(stateLabel) {
    if (stateLabel === 'révoqué') return '#b91c1c';
    if (stateLabel === 'expiré') return '#b45309';
    if (stateLabel === 'initialisation…') return '#64748b';
    return '#166534';
}

function transferProgressFromMessage(status, msg) {
    if (status === 'transferred') return 100;
    if (status === 'ready_for_transfer') return 10;
    if (status !== 'transferring') return 0;
    const text = (msg || '').toLowerCase();
    const m = text.match(/(\\d{1,3})\\s*%/);
    if (m) {
        const v = Math.max(0, Math.min(100, parseInt(m[1], 10)));
        return Number.isFinite(v) ? v : 50;
    }
    if (text.includes('notification')) return 20;
    if (text.includes('téléchargement') || text.includes('telechargement')) return 45;
    if (text.includes('copie')) return 70;
    if (text.includes('finalisation')) return 90;
    return 50;
}

function pipelineProgress(status, statusMessage) {
    const p = { scan: 0, transcode: 0, transfer: 0, error: false, blocked: false, active: 'analyse' };
    switch (status) {
        case 'pending':
            break;
        case 'scanning':
            p.scan = 50;
            p.active = 'analyse';
            break;
        case 'scan_clean':
            p.scan = 100;
            p.active = 'transcodage';
            break;
        case 'scan_infected':
        case 'quarantined':
            p.scan = 100;
            p.blocked = true;
            p.active = 'analyse';
            break;
        case 'transcoding':
            p.scan = 100;
            p.transcode = 50;
            p.active = 'transcodage';
            break;
        case 'transcoded':
            p.scan = 100;
            p.transcode = 100;
            p.active = 'transfert';
            break;
        case 'ready_for_transfer':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = transferProgressFromMessage(status, statusMessage);
            p.active = 'transfert';
            break;
        case 'transferring':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = transferProgressFromMessage(status, statusMessage);
            p.active = 'transfert';
            break;
        case 'transferred':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = 100;
            p.active = 'transfert';
            break;
        case 'transcode_failed':
            p.scan = 100;
            p.transcode = 60;
            p.error = true;
            p.active = 'transcodage';
            break;
        default:
            p.error = true;
            break;
    }
    p.total = Math.round((p.scan + p.transcode + p.transfer) / 3);
    return p;
}

async function generateCode() {
    const btn = document.getElementById('btn-generate');
    btn.disabled = true;
    btn.textContent = 'Génération...';

    try {
        const resp = await fetch('/api/generate-code', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                ttl_minutes: document.getElementById('ttl').value,
                max_uploads: parseInt(document.getElementById('max-uploads').value),
                auto_transcribe: !!(document.getElementById('auto-transcribe') && document.getElementById('auto-transcribe').checked),
            }),
        });
        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.error || 'Erreur serveur');
        }
        const data = await resp.json();

        document.getElementById('display-code').textContent = data.simple_code;
        document.getElementById('qr-img').src = '/api/qr-image/' + data.qr_token;
        document.getElementById('display-expires').textContent =
            'Valide jusqu\\'au ' + new Date(data.expires_at).toLocaleString('fr-FR');
        document.getElementById('display-remaining').textContent =
            `Téléchargements restants: ${data.max_uploads}`;

        document.getElementById('generate-form').style.display = 'none';
        document.getElementById('result').classList.add('active');

        loadSessions();
        loadDevices();
    } catch (e) {
        alert('Erreur: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = 'Générer un code';
    }
}

function resetForm() {
    document.getElementById('generate-form').style.display = 'block';
    document.getElementById('result').classList.remove('active');
}

function updateDeviceFilterButton() {
    const btn = document.getElementById('device-filter-btn');
    if (!btn) return;
    btn.textContent = showAllDevices ? 'Masquer révoqués' : 'Voir révoqués';
}

function toggleDeviceScope() {
    showAllDevices = !showAllDevices;
    updateDeviceFilterButton();
    loadDevices();
}

let pendingDevicesPollTimer = null;

function schedulePendingDevicesPoll(devices) {
    const hasPending = (devices || []).some((d) => (d && d.status ? String(d.status).toLowerCase() : '') === 'pending');
    if (pendingDevicesPollTimer) {
        clearTimeout(pendingDevicesPollTimer);
        pendingDevicesPollTimer = null;
    }
    if (hasPending) {
        // Poll until pending devices either confirm (heartbeat) or are purged
        // server-side. 15s matches the upload-portal heartbeat cadence so the
        // user sees the state transition shortly after it happens.
        pendingDevicesPollTimer = setTimeout(() => {
            pendingDevicesPollTimer = null;
            loadDevices();
        }, 15000);
    }
}

// True after the user clicks "Enrôler un nouvel appareil" or after a generate.
// Persists per browser session so the form stays open while the user iterates.
let userRequestedEnrollmentForm = sessionStorage.getItem('userRequestedEnrollmentForm') === '1';

function showEnrollmentForm() {
    userRequestedEnrollmentForm = true;
    sessionStorage.setItem('userRequestedEnrollmentForm', '1');
    const form = document.getElementById('generate-form');
    const collapsed = document.getElementById('enrollment-collapsed');
    if (form) form.style.display = '';
    if (collapsed) collapsed.style.display = 'none';
}

function applyEnrollmentFormVisibility(devices) {
    // Form stays visible by default — the original "ne plus afficher le QR
    // si un device est enrôlé" was about the QR result (which appears only
    // after a generate click anyway), not the form. Hiding the form behind
    // a toggle confused users who couldn't find the Generate button.
    // Keep the function as a no-op to avoid breaking other call sites.
    const form = document.getElementById('generate-form');
    const collapsed = document.getElementById('enrollment-collapsed');
    if (form) form.style.display = '';
    if (collapsed) collapsed.style.display = 'none';
}

async function loadDevices() {
    const container = document.getElementById('devices-list');
    if (!container) return;
    try {
        const resp = await fetch('/api/my-devices');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur chargement devices');
        const devices = Array.isArray(data) ? data : [];
        schedulePendingDevicesPoll(devices);
        applyEnrollmentFormVisibility(devices);
        const nowMs = Date.now();
        const oneDayMs = 24 * 60 * 60 * 1000;

        const nonRevokedCount = devices.filter((d) => (d.status || '').toLowerCase() !== 'revoked').length;
        // Sélection onglet par défaut au premier chargement (idempotent).
        pickDefaultTab(nonRevokedCount > 0);
        // Populate qr_token map for session header enrichment.
        Object.keys(_devicesByQrToken).forEach(k => delete _devicesByQrToken[k]);
        devices.forEach(d => {
            const qr = (d.qr_token || '').trim();
            if (qr) {
                _devicesByQrToken[qr] = {
                    name: d.device_name || 'Appareil',
                    status: (d.status || '').toLowerCase(),
                };
            }
        });
        const visibleDevices = devices.filter((d) => {
            const status = (d.status || '').toLowerCase();
            if (status !== 'revoked') {
                return true;
            }
            const revokedAtRaw = d.revoked_at || d.updated_at || d.created_at;
            if (!revokedAtRaw) {
                return showAllDevices;
            }
            const revokedAtMs = new Date(revokedAtRaw).getTime();
            if (!Number.isFinite(revokedAtMs)) {
                return showAllDevices;
            }
            const age = nowMs - revokedAtMs;
            if (age >= oneDayMs) return false; // hide after 24h in UI, keep in DB
            return showAllDevices;
        });

        if (!visibleDevices.length) {
            if (showAllDevices) {
                container.innerHTML = `<span style="color:#64748b">Aucun appareil affichable. Appareils enrôlés non révoqués: <strong>${nonRevokedCount}</strong>.</span>`;
            } else {
                container.innerHTML = `<span style="color:#64748b">Aucun appareil enrôlé non révoqué. Compteur: <strong>${nonRevokedCount}</strong>.</span>`;
            }
            return;
        }
        container.innerHTML = visibleDevices.map((d) => {
            const status = (d.status || '').toLowerCase();
            const isRevoked = status === 'revoked';
            const recentUploads24h = Number(d.recent_uploads_24h || 0);
            const remainingUploads = Number(d.remaining_uploads || 0);
            const sessionMaxUploads = Number(d.session_max_uploads || 0);
            const renewNeedsAttention = !isRevoked && (!!d.session_expiring_soon || remainingUploads < 2);
            const stateLabel = deviceTokenStateLabel(d);
            const stateColor = deviceTokenStateColor(stateLabel);
            const tokenShort = (d.session_simple_code || '').trim() || tokenIdShort(d.qr_token);
            // "restants" : seulement affiché quand on approche du quota
            // (< 10), sinon c'est du bruit visuel. Pour un token tout neuf
            // à 999 dispo, ça n'intéresse personne de voir 999/999.
            const remainingFragment = (sessionMaxUploads > 0 && remainingUploads < 10)
                ? ` | restants: ${remainingUploads}/${sessionMaxUploads}`
                : '';
            return `
            <div data-device-row="${escapeHtml(d.device_id)}" class="${isRevoked ? 'device-row-revoked' : ''}" style="border:1px solid #e2e8f0;border-radius:8px;padding:0.55rem 0.6rem;margin-bottom:0.5rem;">
                <div style="display:flex;justify-content:space-between;gap:0.5rem;align-items:center;">
                    <div style="min-width:0;">
                        <div class="device-name" style="font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                            ${escapeHtml(d.device_name || 'Appareil sans nom')}
                            <span class="device-token-code" style="margin-left:0.35rem;" title="${escapeHtml(d.qr_token || '')}">${escapeHtml(tokenShort)}</span>
                            <span data-device-status="${escapeHtml(d.device_id)}" style="font-weight:600;color:${escapeHtml(stateColor)};margin-left:0.35rem;">(${escapeHtml(stateLabel)})</span>
                        </div>
                        <div class="device-meta" style="font-size:0.74rem;color:#64748b;" data-device-meta="${escapeHtml(d.device_id)}">
                            validité token: ${escapeHtml(tokenValidityDaysLabel(d.retention_expires_at))}${remainingFragment} | récents 24h: ${recentUploads24h} | vu: ${escapeHtml(formatDateTimeShort(d.last_seen_at))}
                        </div>
                    </div>
                    <div style="display:flex;gap:0.35rem;align-items:center;">
                        <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary btn-renew-mini ${renewNeedsAttention ? 'btn-renew-alert' : ''}"
                                onclick="renewTokenByQr('${escapeHtml(d.qr_token || '')}')">Renouveller</button>
                        <button class="btn-primary btn-danger-mini fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                                data-device-revoke="${escapeHtml(d.device_id)}"
                                onclick="revokeDevice('${escapeHtml(d.device_id)}')"
                                ${d.status === 'revoked' ? 'disabled' : ''}>Révoquer</button>
                        <button class="btn-primary btn-danger-mini fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                                data-device-delete="${escapeHtml(d.device_id)}"
                                onclick="deleteDevicePermanently('${escapeHtml(d.device_id)}', '${escapeHtml(d.device_name || 'sans nom')}')"
                                title="Suppression irréversible (audit perdu)">Supprimer</button>
                    </div>
                </div>
                <div style="display:flex;gap:0.4rem;margin-top:0.45rem;">
                    <input id="dev-name-${escapeHtml(d.device_id)}" type="text"
                           style="flex:1;padding:0.35rem 0.45rem;border:1px solid #cbd5e1;border-radius:6px;font-size:0.8rem;"
                           placeholder="Renommer l'appareil" value="${escapeHtml(d.device_name || '')}">
                    <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary btn-rename-mini"
                            onclick="renameDevice('${escapeHtml(d.device_id)}')">Renommer</button>
                </div>
            </div>
        `;
        }).join('');
    } catch (e) {
        container.innerHTML = '<span style="color:#b91c1c">Erreur chargement appareils.</span>';
    }
}

async function renameDevice(deviceId) {
    const input = document.getElementById(`dev-name-${deviceId}`);
    if (!input) return;
    const name = (input.value || '').trim();
    if (!name) return;
    try {
        const resp = await fetch(`/api/my-devices/${deviceId}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_name: name }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'rename_failed');
        loadDevices();
    } catch (e) {
        alert('Echec renommage appareil.');
    }
}

async function revokeDevice(deviceId) {
    if (!confirm('Révoquer cet appareil ?')) return;
    const revokeBtn = document.querySelector(`[data-device-revoke="${deviceId}"]`);
    if (revokeBtn) revokeBtn.disabled = true;
    try {
        const resp = await fetch(`/api/my-devices/${deviceId}/revoke`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'revoke_failed');
        const statusEl = document.querySelector(`[data-device-status="${deviceId}"]`);
        if (statusEl) {
            statusEl.textContent = '(révoqué)';
            statusEl.style.color = deviceTokenStateColor('révoqué');
        }
        // Keep the device visible in list, refresh in background for consistency.
        setTimeout(loadDevices, 250);
    } catch (e) {
        if (revokeBtn) revokeBtn.disabled = false;
        alert('Echec révocation appareil.');
    }
}

async function deleteDevicePermanently(deviceId, deviceName) {
    // Double-confirm — irreversible, no audit row left in DB.
    const label = (deviceName || 'sans nom').slice(0, 60);
    if (!confirm(`Supprimer DÉFINITIVEMENT l'appareil « ${label} » ?\n\n` +
                 `Cette action est irréversible : la ligne sera retirée de la base de données ` +
                 `(aucun audit conservé). Pour une suppression réversible, utilisez « Révoquer ».`)) {
        return;
    }
    if (!confirm(`Confirmer la suppression définitive de « ${label} » ?`)) return;
    const btn = document.querySelector(`[data-device-delete="${deviceId}"]`);
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch(`/api/my-devices/${deviceId}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'delete_failed');
        // Remove the row immediately from the DOM and refresh to confirm.
        const row = document.querySelector(`[data-device-row="${deviceId}"]`);
        if (row) row.remove();
        setTimeout(loadDevices, 250);
    } catch (e) {
        if (btn) btn.disabled = false;
        alert('Echec suppression définitive de l\\'appareil.');
    }
}

async function revokeAllDevices() {
    if (!confirm('Révoquer tous vos appareils enrôlés ?')) return;
    try {
        const resp = await fetch('/api/my-devices/revoke-all', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'revoke_all_failed');
        alert(`Appareils révoqués: ${data.revoked || 0}`);
        loadDevices();
    } catch (e) {
        alert('Echec révocation globale.');
    }
}

async function renewTokenByQr(qrToken) {
    if (!qrToken) {
        alert('Token introuvable pour cet appareil.');
        return;
    }
    if (!confirm(`Renouveler ce token pour ${deviceRetentionDays} jours ?`)) return;
    try {
        // On n'envoie PAS ttl_minutes : le serveur applique DEVICE_TOKEN_RETENTION_HOURS
        // par défaut (15j en prod-bêta) pour rester aligné avec la rétention device.
        // Précédemment on envoyait la valeur du select #ttl du form d'enrôlement
        // (5 min par défaut) → l'access expirait 5 min après le renew. Bug.
        const resp = await fetch('/api/my-token/renew-7d', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ qr_token: qrToken }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'renew_failed');
        loadSessions();
        loadDevices();
    } catch (e) {
        alert('Echec renouvellement token.');
    }
}

// (le toggle Voir/Masquer activités a été retiré — la liste est toujours
//  affichée dans l'onglet "Mes transferts et analyses".)

async function purgeSessions() {
    const ok = confirm('Mettre TOUTES vos sessions et leurs fichiers à la corbeille ?\\n\\n' +
                       'Les éléments seront définitivement supprimés au bout de 30 jours.');
    if (!ok) return;
    try {
        const resp = await fetch('/api/purge-my-sessions', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur purge');
        alert(`Mis à la corbeille: ${data.deleted_sessions || 0} session(s), ${data.deleted_files || 0} fichier(s).\\n` +
              `Purge définitive automatique au bout de 30 jours.`);
        loadSessions();
        loadDevices();
    } catch (e) {
        alert('Erreur: ' + e.message);
    }
}

async function deleteFile(fileId, filenameRaw) {
    const filename = (filenameRaw || '').replace(/&#39;/g, "'");
    if (!confirm(`Mettre le fichier « ${filename} » à la corbeille ?\n\n` +
                 `Le fichier (audio + transcription + CR) est masqué de la liste ` +
                 `et sera définitivement supprimé au bout de 30 jours.`)) return;
    // Optimistic UI : on retire la row immédiatement du DOM pour que le user
    // ait un feedback instantané. Si l'API DELETE échoue, on ré-insert la row
    // à sa position d'origine et on alert.
    const row = document.querySelector(`[data-file-row="${fileId}"]`);
    let revertSnapshot = null;
    if (row) {
        revertSnapshot = { el: row, parent: row.parentNode, next: row.nextSibling };
        row.remove();
    }
    // Si on était en vue détail de ce fichier, revenir à la liste.
    if (_detailFileId === fileId) {
        try { showFilesList(); } catch (e) {}
    }
    try {
        const resp = await fetch(`/api/file/${fileId}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'delete_failed');
        // OK : force un loadSessions en arrière-plan pour resync compteurs / autres rows.
        loadSessions({ force: true });
    } catch (e) {
        // Échec API : restaurer la row à sa position d'origine et alerter.
        if (revertSnapshot && revertSnapshot.parent) {
            try {
                if (revertSnapshot.next && revertSnapshot.next.parentNode === revertSnapshot.parent) {
                    revertSnapshot.parent.insertBefore(revertSnapshot.el, revertSnapshot.next);
                } else {
                    revertSnapshot.parent.appendChild(revertSnapshot.el);
                }
            } catch (_) { /* dernière sécurité : loadSessions re-render tout */ }
        }
        alert('Echec suppression du fichier.');
        loadSessions({ force: true });
    }
}

async function deleteSession(simpleCode, allowSilent) {
    // For "expired_unused" / "pending_enrollment" with 0 upload, no confirm
    // (rien à perdre). For "enrolled" / "expired_consumed" with files,
    // double confirm warning that linked files will be removed too.
    if (!allowSilent) {
        if (!confirm(`Mettre la session ${simpleCode} à la corbeille ?\n\n` +
                     `Les fichiers uploadés via ce code seront aussi masqués ` +
                     `et définitivement supprimés au bout de 30 jours.`)) return;
    }
    const btn = document.querySelector(`[data-session-delete="${simpleCode}"]`);
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch(`/api/my-sessions/${simpleCode}`, { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'delete_failed');
        const row = document.querySelector(`[data-session-row="${simpleCode}"]`);
        if (row) row.remove();
        setTimeout(() => { loadSessions(); loadDevices(); }, 250);
    } catch (e) {
        if (btn) btn.disabled = false;
        alert('Echec suppression de la session.');
    }
}

async function renewSession(sessionId) {
    if (!confirm('Renouveler cette session de 7 jours ?')) return;
    try {
        const ttlValue = document.getElementById('ttl') ? document.getElementById('ttl').value : '';
        const addUploadsValue = document.getElementById('max-uploads')
            ? parseInt(document.getElementById('max-uploads').value || '0', 10)
            : 0;
        const resp = await fetch(`/api/my-sessions/${sessionId}/renew-7d`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ttl_minutes: ttlValue,
                add_uploads: addUploadsValue,
            }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'renew_failed');
        loadSessions();
    } catch (e) {
        alert('Echec renouvellement de la session.');
    }
}

// Détection d'interaction utilisateur dans la liste sessions : si focus
// sur un <select>/<input>/<button> dans #sessions-list ou #transfer-live,
// on saute le refresh pour ne pas casser la sélection en cours. Reprise
// au focusout + au prochain tick.
let _userInteractingTs = 0;
document.addEventListener('focusin', (ev) => {
    const t = ev.target;
    if (!t) return;
    if (t.closest('#sessions-list') || t.closest('#transfer-live')) {
        _userInteractingTs = Date.now();
    }
});
document.addEventListener('focusout', () => {
    // Délai pour absorber un clic qui bascule focus rapidement.
    setTimeout(() => { _userInteractingTs = 0; }, 800);
});
function _userIsInteracting() {
    // Considère l'utilisateur actif si focus posé < 2s
    return _userInteractingTs && (Date.now() - _userInteractingTs) < 2000;
}

// Snapshot du dernier rendu (JSON sérialisé) — sert au diff-based refresh
// pour ne pas re-render si rien n'a changé entre 2 polls (ce qui faisait
// flicker l'UI toutes les 15s).
let _lastSessionsSnapshot = '';

async function loadSessions(opts) {
    opts = opts || {};
    // Skip le refresh si l'utilisateur est en train de sélectionner un
    // download ou cliquer un bouton — sinon innerHTML remplace le DOM
    // et la sélection est perdue. Le polling 15s tentera de nouveau.
    if (_userIsInteracting() && !opts.force) return;
    // Préserve la position de scroll pendant le refresh des sessions
    // (sinon innerHTML reset le scroll en haut, particulièrement gênant
    // sur les pages longues avec plusieurs sessions actives).
    const savedScrollY = window.scrollY;
    // Sauvegarde aussi l'ID + value du select de download en focus, si y'en
    // a un (l'utilisateur a peut-être hover/sélectionné mais pas encore
    // cliqué un bouton — restaure pour pas perdre le fil).
    window._savedDlSelections = new Map();
    document.querySelectorAll('.downloads-select').forEach((sel) => {
        const sec = sel.closest('.transcript-section');
        const fid = sec && sec.getAttribute('data-transcript-file-id');
        if (fid) window._savedDlSelections.set(fid, sel.selectedIndex);
    });
    try {
        const resp = await fetch('/api/my-sessions');
        const sessions = await resp.json();
        if (!resp.ok) {
            throw new Error((sessions && sessions.error) ? sessions.error : 'Erreur API sessions');
        }
        if (!Array.isArray(sessions)) {
            throw new Error('Format API invalide');
        }
        // Diff : si la response est identique à la dernière (et qu'aucune
        // vue détail/forced n'est demandée), on skip le re-render pour
        // éviter le flicker visuel toutes les 15s sur une page stable.
        const snap = JSON.stringify(sessions);
        if (!opts.force && snap === _lastSessionsSnapshot) return;
        _lastSessionsSnapshot = snap;
        const container = document.getElementById('sessions-list');
        const transferBox = document.getElementById('transfer-live');
        const activityMiniText = document.getElementById('activity-mini-text');
        const activityRail = document.getElementById('activity-rail');
        const activitySpinner = document.getElementById('activity-spinner');

        const activityStats = { analyse: 0, transcodage: 0, transfert: 0, done: 0, blocked: 0, total: 0 };
        for (const s of sessions) {
            for (const f of (s.uploads || [])) {
                activityStats.total += 1;
                switch (f.status) {
                    case 'pending':
                    case 'scanning':
                    case 'scan_clean':
                        activityStats.analyse += 1;
                        break;
                    case 'transcoding':
                    case 'transcoded':
                        activityStats.transcodage += 1;
                        break;
                    case 'ready_for_transfer':
                    case 'transferring':
                        activityStats.transfert += 1;
                        break;
                    case 'transferred':
                        activityStats.done += 1;
                        break;
                    case 'scan_infected':
                    case 'quarantined':
                    case 'transcode_failed':
                    case 'error':
                        activityStats.blocked += 1;
                        break;
                    default:
                        activityStats.analyse += 1;
                        break;
                }
            }
        }
        if (activityMiniText) {
            if (activityStats.total === 0) {
                activityMiniText.textContent = 'Activités: aucune.';
            } else {
                const parts = [
                    `A ${activityStats.analyse}`,
                    `T ${activityStats.transcodage}`,
                    `X ${activityStats.transfert}`,
                ];
                if (activityStats.blocked > 0) parts.push(`Q ${activityStats.blocked}`);
                if (activityStats.done > 0) parts.push(`OK ${activityStats.done}`);
                activityMiniText.textContent = `Activités: ${parts.join(' | ')}`;
            }
        }
        if (activitySpinner) {
            const active = (activityStats.analyse + activityStats.transcodage + activityStats.transfert) > 0;
            activitySpinner.classList.toggle('active', active);
            activitySpinner.title = active ? 'Activité en cours' : 'Aucune activité en cours';
        }
        if (activityRail) {
            const analyseOn = activityStats.analyse > 0;
            const transcodeOn = activityStats.transcodage > 0;
            const transferOn = activityStats.transfert > 0;
            activityRail.innerHTML = `
                <span class="activity-dot ${analyseOn ? 'active' : ''}" title="Analyse: ${activityStats.analyse}">1</span>
                <span class="activity-link ${(analyseOn || transcodeOn) ? 'active' : ''}"></span>
                <span class="activity-dot ${transcodeOn ? 'active' : ''}" title="Transcodage: ${activityStats.transcodage}">2</span>
                <span class="activity-link ${(transcodeOn || transferOn) ? 'active' : ''}"></span>
                <span class="activity-dot ${transferOn ? 'active' : ''}" title="Transfert: ${activityStats.transfert}">3</span>
            `;
        }

        // Files "in progress" = tout ce qui n'est pas encore "transferred"
        // (pré-transfert + transfer en cours). Pour chaque on affiche :
        //   ligne 1 : nom + status_message (ex: "Transcription Kevent — file
        //             d'attente Mirai")
        //   ligne 2 : mini chemin de fer 4 étapes (analyse / transcodage /
        //             transfert / transcription)
        // Disparaît dès que le fichier passe en transferred (la suite est
        // visible dans la liste sessions plus bas).
        const transfersInProgress = sessions.flatMap(s =>
            ((s.uploads || []).map(f => ({
                fileId: f.id,
                sessionCode: s.simple_code,
                name: f.original_filename,
                status: f.status,
                message: f.status_message || '',
                updatedAt: f.updated_at || f.created_at || null,
            })))
        ).filter(f => f.status !== 'transferred'
                   && f.status !== 'error'
                   && f.status !== 'scan_infected'
                   && f.status !== 'quarantined'
                   && f.status !== 'transcode_failed');

        if (transferBox) {
            if (transfersInProgress.length === 0) {
                transferBox.style.display = 'none';
                transferBox.innerHTML = '';
            } else {
                transferBox.style.display = '';
                const now = Date.now();
                const rows = transfersInProgress.map(t => {
                    const progress = pipelineProgress(t.status, t.message);
                    const analyseClass = (progress.scan === 100 && !progress.blocked) ? 'done'
                        : (progress.active === 'analyse' ? (progress.blocked ? 'blocked' : 'active') : '');
                    const transcodeClass = (progress.transcode === 100) ? 'done'
                        : (progress.active === 'transcodage' ? (progress.error ? 'blocked' : 'active') : '');
                    const transferClass = (progress.transfer === 100) ? 'done'
                        : (progress.active === 'transfert' ? 'active' : '');
                    const stale = t.updatedAt && (now - new Date(t.updatedAt).getTime()) > 180000;
                    return `
                        <div class="transfer-live-row" title="${escapeHtml(t.name)}">
                            <div class="transfer-live-line1">
                                <span class="transfer-live-code">${escapeHtml(t.sessionCode)}</span>
                                <span class="transfer-live-name">${escapeHtml(t.name)}</span>
                                <span class="transfer-live-msg" style="color:${stale ? '#b91c1c' : '#64748b'};">
                                    ${escapeHtml(t.message || 'En cours...')}
                                </span>
                            </div>
                            <div class="railroad railroad-mini">
                                <div class="rail-segment ${analyseClass}">
                                    <span class="rail-node">1</span><span class="rail-line"></span>
                                </div>
                                <div class="rail-segment ${transcodeClass}">
                                    <span class="rail-node">2</span><span class="rail-line"></span>
                                </div>
                                <div class="rail-segment ${transferClass}">
                                    <span class="rail-node">3</span><span class="rail-line"></span>
                                </div>
                                <div class="rail-segment" data-transcribe-segment="${t.fileId}">
                                    <span class="rail-node">4</span><span class="rail-line rail-line-tail"></span>
                                </div>
                            </div>
                        </div>`;
                }).join('');
                transferBox.innerHTML = `
                    <div class="transfer-live-title">Transferts en cours (${transfersInProgress.length})</div>
                    <div class="transfer-live-list">${rows}</div>
                `;
            }
        }

        if (sessions.length === 0) {
            container.innerHTML = '<p style="color:#999;font-size:0.85rem;">Aucune session</p>';
            const purgeBtn = document.getElementById('purge-btn');
            if (purgeBtn) purgeBtn.disabled = true;
            return;
        }

        // En mode vue détail, on garde uniquement la session qui contient
        // le fichier ciblé, et on filtre tout le reste (autres sessions,
        // wrappers buckets) pour vraiment afficher une page focus fichier.
        const sessionsToRender = _detailFileId
            ? sessions.filter(s => (s.uploads || []).some(u => u.id === _detailFileId))
            : sessions;

        const renderedItems = sessionsToRender.map(s => {
            const isActive = s.status === 'active' && new Date(s.expires_at) > new Date();
            const statusClass = isActive ? 'status-active' : 'status-expired';
            const sessionStatusLabel = isActive ? 'Actif' : 'Expiré';
            const remainingDownloads = Math.max(0, (s.max_uploads || 0) - (s.upload_count || 0));
            const recentUploadsCount = (s.uploads || []).filter((f) => {
                const created = new Date(f.created_at || f.updated_at || 0).getTime();
                if (!Number.isFinite(created) || created <= 0) return false;
                return (Date.now() - created) <= (24 * 60 * 60 * 1000);
            }).length;
            const expiresAtMs = new Date(s.expires_at).getTime();
            const expiringSoon = Number.isFinite(expiresAtMs)
                && (expiresAtMs - Date.now()) <= (2 * 24 * 60 * 60 * 1000);
            const renewNeedsAttention = remainingDownloads < 2 || expiringSoon;

            const filesHtml = (s.uploads || []).map(f => {
                // Si une vue détail est active et ce fichier n'est pas le
                // détail demandé, on le saute (un seul fichier visible).
                if (_detailFileId && _detailFileId !== f.id) return '';
                const isDetailView = (_detailFileId === f.id);
                const quality = (f.audio_quality_score !== null && f.audio_quality_score !== undefined)
                    ? ` <span class="quality-help" title="Indice de qualité audio (1 à 5). Calculé automatiquement par le worker de transcodage selon le niveau RMS, la proportion de silence, la durée et la fréquence d'échantillonnage.">i</span> ${f.audio_quality_score.toFixed(1)}/5`
                    : '';
                const progress = pipelineProgress(f.status, f.status_message);
                const fileStatusClass = `file-badge-${f.status || 'pending'}`;
                const fileDateLabel = _formatDateCompact(f.created_at);
                const fileDurLabel = _formatDuration(f.audio_duration_seconds);
                const analyseClass = (progress.scan === 100 && !progress.blocked) ? 'done'
                    : (progress.active === 'analyse' ? (progress.blocked ? 'blocked' : 'active') : '');
                const transcodeClass = (progress.transcode === 100) ? 'done'
                    : (progress.active === 'transcodage' ? (progress.error ? 'blocked' : 'active') : '');
                const transferClass = (progress.transfer === 100) ? 'done'
                    : (progress.active === 'transfert' ? 'active' : '');
                const canComputeImpact = (f.status === 'transcoded' || f.status === 'transferring' || f.status === 'transferred');
                const cache = impactCache[f.id];
                const loading = impactLoading.has(f.id);
                const impactTooltip = loading
                    ? 'Analyse en cours...'
                    : (cache
                        ? `${cache.text} (Maj: ${cache.at})`
                        : 'Impact non calculé. Cliquez sur cette icône pour calculer et afficher l\\'impact de la normalisation.');
                const impactIcon = canComputeImpact
                    ? `<button class="impact-icon-btn ${loading ? 'loading' : (cache ? 'computed' : '')}"
                           onclick="loadNormalizationImpact('${f.id}')"
                           title="${escapeHtml(impactTooltip)}"
                           ${loading ? 'disabled' : ''}>i</button>`
                    : '';
                // Liste des audios téléchargeables pour ce fichier — sera
                // mergée avec les transcripts par loadTranscriptStatus pour
                // produire un seul dropdown au lieu de 3 blocs Source/
                // Transcodé/Transféré qui prenaient toute la hauteur.
                const audioDownloadsList = [];
                if (f.source_available) audioDownloadsList.push({
                    label: 'Audio source', dl: f.source_download_url, stream: f.source_stream_url,
                });
                if (f.transcoded_available) audioDownloadsList.push({
                    label: 'Audio transcodé', dl: f.transcoded_download_url, stream: f.transcoded_stream_url,
                });
                if (f.transferred_available) audioDownloadsList.push({
                    label: 'Audio transféré (interne)', dl: f.transferred_download_url, stream: f.transferred_stream_url,
                });
                const audioDownloadsAttr = encodeURIComponent(JSON.stringify(audioDownloadsList));
                // Cache le chemin de fer une fois le pipeline pré-transcription
                // abouti (fichier transféré côté interne). La 4e étape
                // (transcription IA) est affichée séparément par le bandeau
                // de loadTranscriptStatus, donc plus besoin du rail visuel
                // qui prend de la place. On laisse le rail visible pendant
                // l'analyse / transcodage / transfert pour montrer où on en
                // est en temps réel.
                const pipelineDone = (f.status === 'transferred');
                const railroadBlock = pipelineDone ? '' : `
                    <div class="pipeline-box" title="Progression du pipeline en chemin de fer: analyse, transcodage, transfert, transcription">
                        <div class="railroad">
                            <div class="rail-segment ${analyseClass}">
                                <span class="rail-node">1</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment ${transcodeClass}">
                                <span class="rail-node">2</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment ${transferClass}">
                                <span class="rail-node">3</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment" data-transcribe-segment="${f.id}">
                                <span class="rail-node">4</span><span class="rail-line rail-line-tail"></span>
                            </div>
                        </div>
                        <div class="rail-labels">
                            <span>Analyse ${progress.scan}%</span>
                            <span>Transcodage ${progress.transcode}%</span>
                            <span>Transfert ${progress.transfer}%</span>
                            <span data-transcribe-label="${f.id}">Transcription</span>
                        </div>
                    </div>`;
                // Bandeau d'alerte rouge inline si l'antivirus a bloqué le fichier
                // (scan_infected / quarantined). On l'insère AVANT le contenu
                // normal de la row pour que ce soit la première chose lue par
                // l'utilisateur. Le dot rouge fixe + tooltip clair complètent.
                const VIRUS_STATES = new Set(['scan_infected', 'quarantined']);
                const isVirusBlocked = VIRUS_STATES.has(f.status);
                const virusBanner = isVirusBlocked
                    ? `<div class="file-row-virus-banner" role="alert">
                         <span class="file-row-virus-icon" aria-hidden="true">⚠</span>
                         <span class="file-row-virus-msg">
                           <strong>Virus détecté</strong> — fichier
                           ${f.status === 'quarantined' ? 'mis en quarantaine' : 'bloqué par l\\'antivirus'}.
                           Aucun téléchargement possible. Si vous pensez à un
                           faux positif, contactez un administrateur.
                         </span>
                       </div>`
                    : '';
                if (!isDetailView) {
                    // ── Vue LISTE COMPACTE ─────────────────────────────
                    // Une seule ligne + chevron expandable pour le résumé :
                    //   • point statut transcription (rollover = label complet)
                    //   • titre cliquable (= suggested_filename si dispo, sinon
                    //     filename technique) — ouvre vue détail
                    //   • date+durée
                    //   • chevron ▶ : déplie inline le résumé sans quitter la liste
                    //   • bouton Supprimer
                    return `<div class="file-row-compact-wrapper${isVirusBlocked ? ' file-row-virus' : ''}" data-file-row="${f.id}">
                        ${virusBanner}
                        <div class="file-row-compact">
                            <!-- transcript-section caché : sert juste à
                                 déclencher loadTranscriptStatus qui mettra
                                 à jour la couleur du dot inline via JS. -->
                            <div class="transcript-section transcript-section--inline"
                                 data-transcript-file-id="${f.id}"
                                 data-audio-downloads="${audioDownloadsAttr}"
                                 data-compact="1"
                                 style="display:none;"></div>
                            <!-- Dot inline (caractère unicode) : aligné comme
                                 un caractère sur la baseline du titre. Sa
                                 couleur+animation est ajustée par
                                 loadTranscriptStatus via la classe
                                 file-row-dot-<status>. À l'init, on pose
                                 file-row-dot-upload-in-progress tant que
                                 l'upload n'est pas TRANSFERRED — ça suffit à
                                 animer "il se passe un truc" avant même que
                                 la transcription démarre. -->
                            <span class="file-row-dot ${UPLOAD_IN_PROGRESS_STATES.has(f.status) ? 'file-row-dot-upload-in-progress' : ''} ${isVirusBlocked ? `file-row-dot-${f.status}` : ''}"
                                  data-file-dot="${f.id}"
                                  title="${escapeHtml(isVirusBlocked ? `Virus détecté — ${statusLabel(f.status)}` : _uploadStateLabel(f.status))}">●</span>
                            <a href="#" class="file-row-title" data-file-id="${f.id}"
                               onclick="event.preventDefault();showFileDetail('${f.id}');"
                               title="${escapeHtml(f.original_filename)}">
                                ${escapeHtml(f.original_filename)}
                            </a>
                            <!-- Hint file d'attente Kevent (visible uniquement
                                 quand le pipeline est en cours — peuplé par
                                 _pollQueueHintAll via /api/queue-status,
                                 vide sinon). -->
                            <span class="file-row-queue-hint"
                                  data-queue-hint-for="${f.id}"></span>
                            <button class="file-row-expand" type="button"
                                    onclick="toggleRowExpand(this)"
                                    aria-label="Voir le résumé">
                                <span class="file-row-expand-icon">▶</span>
                                <span class="file-row-expand-label">détails</span>
                            </button>
                            <span class="file-row-meta">
                                <span class="file-row-meta-date">${escapeHtml(fileDateLabel)}</span>
                                ${fileDurLabel ? `<span class="file-row-meta-dur">${escapeHtml(fileDurLabel)}</span>` : ''}
                            </span>
                            <button type="button" class="icon-btn file-row-delete"
                                    onclick="deleteFile('${f.id}', '${escapeHtml(f.original_filename).replace(/'/g, '&#39;')}')"
                                    title="Mettre à la corbeille (purgée définitivement après 30 jours)"
                                    aria-label="Mettre à la corbeille">
                                ${ICONS.trash}
                            </button>
                        </div>
                        <!-- Zone résumé révélée par le chevron, sans le statut
                             technique (qui reste accessible via tooltip de la
                             pastille). -->
                        <div class="file-row-expanded" data-expanded-file-id="${f.id}" style="display:none;">
                            <div class="file-row-expanded-summary"
                                 data-expanded-summary-for="${f.id}"></div>
                        </div>
                    </div>`;
                }
                // ── Vue DÉTAIL ──────────────────────────────────────────
                // Mode "page" : on cache tout le reste (header de la session
                // + autres fichiers) via la classe parent `.detail-active`
                // pour ne montrer QUE le détail demandé.
                // Titre éditable (suggested_filename, persisté via
                // /api/file/<id>/rename) + nom technique en petit dessous.
                // Statut technique uniquement via tooltip sur la pastille.
                // Résumé déployé persistant (pas de <details>).
                return `<div class="file-detail${isVirusBlocked ? ' file-detail-virus' : ''}" data-detail-file-id="${f.id}">
                    ${virusBanner}
                    <div class="file-detail-header">
                        <button type="button" class="file-detail-back"
                                onclick="showFilesList()"
                                title="Retour à la liste des réunions"
                                aria-label="Retour à la liste">← Liste</button>
                        <button type="button" class="icon-btn"
                                onclick="deleteFile('${f.id}', '${escapeHtml(f.original_filename).replace(/'/g, '&#39;')}')"
                                title="Mettre à la corbeille (purgée définitivement après 30 jours)"
                                aria-label="Mettre à la corbeille">
                            ${ICONS.trash}
                        </button>
                    </div>
                    <div class="file-detail-title-row">
                        <input class="file-detail-title-input" type="text"
                               value="${escapeHtml(f.original_filename)}"
                               data-original-title="${escapeHtml(f.original_filename)}"
                               data-detail-title-for="${f.id}"
                               placeholder="Titre de la réunion" />
                        <button class="file-detail-rename-btn fr-btn fr-btn--sm fr-btn--secondary"
                                onclick="renameDetailTitle('${f.id}', this)" disabled>
                            Renommer
                        </button>
                    </div>
                    <!-- Ligne sous le titre : juste date+durée à gauche +
                         bouton (i) coloré à droite. Les infos techniques
                         (qualité, statut, étapes, normalisation) sont
                         derrière le bouton (i) qui ouvre un modal. -->
                    <div class="file-detail-techline">
                        <span class="file-row-meta">
                            <span class="file-row-meta-date">${escapeHtml(fileDateLabel)}</span>
                            ${fileDurLabel ? `<span class="file-row-meta-dur">${escapeHtml(fileDurLabel)}</span>` : ''}
                        </span>
                        <span class="file-detail-source-filename"
                              title="Nom d'origine du fichier audio">
                            ${escapeHtml(f.original_filename)}
                        </span>
                        <button class="file-detail-info-btn"
                                type="button"
                                data-file-info-btn="${f.id}"
                                onclick="openFileInfoModal('${f.id}')"
                                title="Détails techniques (statut, qualité, étapes IA, normalisation)"
                                aria-label="Voir les détails techniques">i</button>
                    </div>
                    <!-- transcript-section caché pour déclencher
                         loadTranscriptStatus qui met à jour la couleur du
                         bouton (i) selon le status. -->
                    <div class="transcript-section transcript-section--inline"
                         data-transcript-file-id="${f.id}"
                         data-audio-downloads="${audioDownloadsAttr}"
                         data-compact="1"
                         data-info-btn-target="${f.id}"
                         style="display:none;"></div>
                    <!-- Hint file d'attente Kevent (idem PWA) — poll 10s tant
                         que la transcription n'est pas terminée. -->
                    <p class="fr-text--sm fr-text-mention--grey queue-hint"
                       data-queue-hint-for="${f.id}"
                       style="margin:.4rem 0 0 0;min-height:1.1em;"></p>
                    ${railroadBlock}
                    <!-- Section résumé toujours visible (pas de <details>
                         repliable en vue détail). Le contenu (key_points
                         + dropdown downloads) est injecté par
                         loadTranscriptStatus en mode non-compact. -->
                    <div class="file-detail-fullinfo transcript-section"
                         data-transcript-file-id="${f.id}"
                         data-audio-downloads="${audioDownloadsAttr}"
                         data-persistent-summary="1"></div>
                </div>`;
            }).join('');

                // Lifecycle bucket from server-computed state. UI labels +
                // delete-button confirmation severity vary per bucket.
                const lifecycle = s.lifecycle_state || (isActive ? 'pending_enrollment' : 'expired_unused');
                const labels = {
                    pending_enrollment: 'En attente d\\'enrôlement',
                    enrolled:           'Enrôlé',
                    expired_unused:     'Inutilisée (jetable)',
                    expired_consumed:   'Expirée — fichiers conservés',
                };
                const stateLabel = labels[lifecycle] || sessionStatusLabel;
                const stateBadgeClass = lifecycle === 'enrolled' ? 'status-active'
                    : lifecycle === 'pending_enrollment' ? 'status-active'
                    : 'status-expired';
                const allowSilentDelete = (lifecycle === 'expired_unused' || lifecycle === 'pending_enrollment')
                    && (s.upload_count || 0) === 0;

                // Sessions actives (enrolled / pending_enrollment) : UI
                // épurée — on cache les actions globales (renouveler,
                // supprimer-session) et le compteur de quota qui n'a pas de
                // valeur pour l'utilisateur quotidien (la rétention device
                // pilote, et le quota est haut). Tout reste accessible dans
                // le bucket "Sessions inutilisées (jetables)" où la session
                // est figée/morte.
                const isActiveBucket = (lifecycle === 'pending_enrollment' || lifecycle === 'enrolled');
                const sessionDeleteBtn = isActiveBucket ? '' : `
                    <button type="button" class="icon-btn"
                            data-session-delete="${s.simple_code}"
                            onclick="deleteSession('${s.simple_code}', ${allowSilentDelete})"
                            title="Mettre cette session à la corbeille (purgée définitivement après 30 jours)"
                            aria-label="Mettre la session à la corbeille">${ICONS.trash}</button>`;
                const sessionRenewBtn = isActiveBucket ? '' : `
                    <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary btn-renew-mini ${renewNeedsAttention ? 'btn-renew-alert' : ''}"
                            onclick="renewSession('${s.id}')">Renouveller</button>`;
                const quotaLine = isActiveBucket ? '' : `
                    <span style="float:right;color:#888;">restants: ${remainingDownloads} (utilisés: ${s.upload_count}/${s.max_uploads}) | récents 24h: ${recentUploadsCount}</span>`;
                // Header : on remplace "CODE + Enrôlé" par
                // "DeviceName CODE (status)" inspiré de l'onglet
                // "Mes appareils". Fallback sur l'ancien format si aucun
                // device matching trouvé (cas pending_enrollment).
                const dev = _devicesByQrToken[(s.qr_token || '')];
                const headerInner = dev
                    ? `<span class="session-device-name">${escapeHtml(dev.name)}</span>
                       <span class="code">${escapeHtml(s.simple_code)}</span>
                       <span class="status-badge ${stateBadgeClass}">(${escapeHtml(dev.status)})</span>`
                    : `<span class="code">${escapeHtml(s.simple_code)}</span>
                       <span class="status-badge ${stateBadgeClass}">${escapeHtml(stateLabel)}</span>`;
                return `<div class="session-item" data-session-row="${s.simple_code}" data-lifecycle="${lifecycle}">
                ${headerInner}
                ${sessionRenewBtn}
                ${sessionDeleteBtn}
                ${quotaLine}
                ${filesHtml}
            </div>`;
            });

        // Bucket sessions by lifecycle for visual grouping. `expired_consumed`
        // (QR grace passée mais fichiers uploadés + device enrôlé) reste dans
        // le bucket actif : l'utilisateur veut écouter/voir ses fichiers
        // tant que le device est valide (rétention 15j), pas devoir déplier
        // une section repliée par défaut.
        const buckets = { active: [], obsolete: [] };
        sessionsToRender.forEach((s, i) => {
            const lc = s.lifecycle_state;
            const html = renderedItems[i];
            if (lc === 'expired_unused') buckets.obsolete.push(html);
            else buckets.active.push(html);
        });
        const collapsibleSection = (title, items, id) => items.length === 0 ? '' : `
            <details id="${id}" style="margin-top:0.5rem;">
                <summary style="cursor:pointer;font-size:0.84rem;color:#475569;padding:0.3rem 0;">
                    ${title} (${items.length})
                </summary>
                <div style="margin-top:0.4rem;">${items.join('')}</div>
            </details>`;
        container.innerHTML = `
            ${buckets.active.join('')}
            ${collapsibleSection('Sessions inutilisées (jetables)', buckets.obsolete, 'obsolete-sessions')}
        `;

        const fileCount = sessions.reduce((acc, s) => acc + ((s.uploads || []).length), 0);
        const purgeBtn = document.getElementById('purge-btn');
        if (purgeBtn) purgeBtn.disabled = fileCount === 0;

        // Lazy fetch transcript metadata for each file row to populate the
        // download section + key_points subtitle. Throttled by browser parallel
        // limit; fired and forgotten — failures leave the section empty.
        document.querySelectorAll('[data-transcript-file-id]').forEach((el) => {
            const fid = el.dataset.transcriptFileId;
            if (fid && !el.dataset.loaded) {
                el.dataset.loaded = '1';
                loadTranscriptStatus(fid, el);
            }
        });
    } catch (e) {
        console.error('Failed to load sessions', e);
        const container = document.getElementById('sessions-list');
        const activityMiniText = document.getElementById('activity-mini-text');
        const activitySpinner = document.getElementById('activity-spinner');
        if (container) {
            container.innerHTML = '<p style="color:#b91c1c;font-size:0.85rem;">Erreur chargement sessions. Rechargez la page.</p>';
        }
        if (activityMiniText) {
            activityMiniText.textContent = 'Activités: indisponibles.';
        }
        if (activitySpinner) {
            activitySpinner.classList.remove('active');
            activitySpinner.title = 'Activités indisponibles';
        }
    } finally {
        // Restaure la position de scroll après l'innerHTML, sinon la
        // page remonte en haut à chaque polling 15s.
        if (Number.isFinite(savedScrollY)) {
            requestAnimationFrame(() => window.scrollTo(0, savedScrollY));
        }
    }
}

// ─── Transcript / CR downloads (Feature 3) ─────────────────────────────────
// For each file row we lazy-load the available outputs (transcript, corrected,
// CR) and render a download row + key_points subtitle. One HTTP per file —
// acceptable since we display ~10-20 files and the endpoint is internal-only.
//
// TODO follow-up: button to send the rendered document to the user's Drive
// folder. Out of scope for this PR — needs OAuth scope + Drive provider config.

const TRANSCRIPT_KIND_LABELS = {
    'transcript':              'Transcription brute',
    'transcript-tagged':       'Transcription par locuteur',
    'transcript-corrected':    'Transcription (sigles corrigés)',
    'transcript-cleaned':      'Transcription nettoyée',
    'transcript-reformulated': 'Discours indirect',
};

const TRANSCRIPT_KIND_FORMATS = {
    'transcript':              ['txt', 'md', 'docx', 'odt'],
    'transcript-tagged':       ['md', 'docx', 'odt'],
    'transcript-corrected':    ['md', 'docx', 'odt'],
    'transcript-cleaned':      ['txt', 'md', 'docx', 'odt'],
    'transcript-reformulated': ['md', 'docx', 'odt'],
};

const CR_FORMATS = ['md', 'docx', 'odt', 'json'];

// Map globale { selectId → [iconsHtml par index d'option] } pour éviter
// les pièges d'escape HTML quand on stocke du HTML dans un attribut.
const _otherDlIcons = new Map();

// Met à jour la rangée d'icônes à droite du select "Autres téléchargements".
// On résout le target via le DOM voisin (sel.closest(.downloads-other-row))
// et non via document.querySelector — plusieurs containers transcript-section
// peuvent partager le même selectId (vue compacte + vue détail) et un
// querySelector global retourne le PREMIER (souvent l'élément caché).
function updateOtherDownload(sel) {
    const row = sel.closest('.downloads-other-row');
    const target = row && row.querySelector('.downloads-other-icons');
    if (!target) return;
    const list = _otherDlIcons.get(sel.id) || [];
    const idx = sel.selectedIndex;
    target.innerHTML = (idx >= 0 && list[idx]) || '';
}

// Met à jour les boutons Télécharger/Écouter selon l'option sélectionnée
// dans le dropdown unifié (audios + transcripts + meeting-cr).
function updateDownloadButtons(select) {
    const opt = select.options[select.selectedIndex];
    if (!opt) return;
    const dl = opt.getAttribute('data-dl') || '#';
    const stream = opt.getAttribute('data-stream') || '';
    const isAudio = !!opt.getAttribute('data-audio');
    const block = select.closest('.downloads-block');
    if (!block) return;
    const dlBtn = block.querySelector('.downloads-btn-dl');
    const streamBtn = block.querySelector('.downloads-btn-stream');
    if (dlBtn) dlBtn.setAttribute('href', dl);
    if (streamBtn) {
        if (isAudio && stream) {
            streamBtn.setAttribute('href', stream);
            streamBtn.style.display = '';
        } else {
            streamBtn.style.display = 'none';
        }
    }
}

function renderDownloadRow(label, fileId, kind, formats, isCR) {
    const buttons = formats.map((ext) => {
        const url = isCR
            ? `/api/file/meeting-cr/${ext}/${fileId}`
            : `/api/file/transcript/${kind}/${ext}/${fileId}`;
        return `<a class="transcript-fmt-btn" href="${url}" target="_blank" rel="noopener" download>.${ext}</a>`;
    }).join('');
    return `<div class="transcript-download-row">
        <span class="transcript-download-label">${escapeHtml(label)}</span>
        <span class="transcript-download-buttons">${buttons}</span>
    </div>`;
}

// Map des statuts transcription DB → label UI + indique si on doit re-poll.
// Met à jour la 4e étape du chemin de fer (rail-segment + label associé)
// selon le backend de transcription en cours. Appelé depuis
// loadTranscriptStatus() pour rester cohérent avec ce que voit
// l'utilisateur dans le bandeau statut.
function updateTranscribeRail(fileId, engine, status) {
    const segment = document.querySelector(`[data-transcribe-segment="${fileId}"]`);
    const label = document.querySelector(`[data-transcribe-label="${fileId}"]`);
    if (!segment || !label) return;
    const e = (engine || '').toLowerCase();
    const s = (status || '').toLowerCase();
    // Nom utilisateur du backend
    const engineNames = {
        stub: 'Transcription (test)',
        mcr:  'Transcription MCR',
        kevent: 'Transcription IA',
    };
    const baseName = engineNames[e] || 'Transcription';
    let cls = '';
    let label_text = baseName;
    if (s === 'completed' || s === 'kevent_completed' || s === 'mcr_pushed') {
        cls = 'done';
        label_text = `${baseName} terminée`;
    } else if (s === 'kevent_partially_completed') {
        cls = 'done';
        label_text = `${baseName} (partielle)`;
    } else if (s === 'failed' || s === 'kevent_failed' || s === 'mcr_auth_failed' || s === 'mcr_rejected' || s === 'mcr_push_failed') {
        cls = 'blocked';
        label_text = `${baseName} échouée`;
    } else if (s === 'disabled') {
        cls = '';
        label_text = `${baseName} désactivée`;
    } else if (s) {
        cls = 'active';
        label_text = `${baseName} en cours`;
    }
    segment.className = `rail-segment ${cls}`;
    label.textContent = label_text;
}

const TRANSCRIPT_STATUS_LABELS = {
    'pending':                     { label: 'Transcription en attente', polling: true },
    'processing':                  { label: 'Transcription en cours (stub)', polling: true },
    'completed':                   { label: 'Transcription disponible', polling: false },
    'failed':                      { label: 'Transcription échouée', polling: false },
    'kevent_queued':               { label: 'Transcription Kevent — file d\\'attente Mirai', polling: true },
    'kevent_transcribing':         { label: 'Transcription Kevent — Whisper en cours', polling: true },
    'kevent_processing':           { label: 'Transcription Kevent — traitement', polling: true },
    'kevent_completed':            { label: 'Pipeline Kevent terminé', polling: false },
    'kevent_partially_completed':  { label: 'Pipeline Kevent partiel — certaines étapes ont échoué', polling: false },
    'kevent_failed':               { label: 'Accès au backend IA refusé ou indisponible', polling: false },
    'mcr_pushed':                  { label: 'Poussé vers MCR', polling: false },
    'mcr_auth_failed':             { label: 'MCR : échec auth', polling: false },
    'mcr_rejected':                { label: 'MCR : rejeté', polling: false },
    'mcr_push_failed':             { label: 'MCR : échec push', polling: false },
    'disabled':                    { label: 'Transcription désactivée', polling: false },
};

// Étapes du pipeline IA — ordre d'exécution. Réutilisé pour construire la
// checklist de progression dans le tooltip du (i) et le modal Détails.
const PIPELINE_STEPS = [
    { key: 'transcript',              label: 'Transcription brute' },
    { key: 'transcript-tagged',       label: 'Identification des locuteurs' },
    { key: 'transcript-corrected',    label: 'Correction des sigles' },
    { key: 'transcript-cleaned',      label: 'Nettoyage hors-sujet' },
    { key: 'transcript-reformulated', label: 'Discours indirect' },
    { key: 'meeting-cr',              label: 'Compte-rendu structuré' },
];

const _FAILED_TRANSCRIPT_STATUSES = new Set([
    'failed', 'kevent_failed',
    'mcr_auth_failed', 'mcr_rejected', 'mcr_push_failed',
]);

// Construit le tooltip multi-ligne du bouton (i). Chaque étape porte un
// glyphe :
//   ✓  étape réussie (output présent)
//   ✗  étape échouée explicitement (statut failed + output absent)
//   ⏳  étape en cours (pipeline qui tourne + output absent)
//   ☐  étape en attente (pas encore tentée)
// Les sauts de ligne \\n sont rendus par les tooltips natifs (vu sur
// Firefox/Chrome desktop).
function _buildInfoTooltip(status, engine, outputs, meta) {
    const head = (meta && meta.label) || status || 'Statut inconnu';
    const isFail = _FAILED_TRANSCRIPT_STATUSES.has(status);
    const isRunning = !!(meta && meta.polling);
    const lines = [
        `Pipeline IA — ${head}${engine ? ' (' + engine + ')' : ''}`,
        '─────────────',
    ];
    // Première étape "en cours" qu'on rencontre = la prochaine attendue.
    let firstPendingMarked = false;
    for (const step of PIPELINE_STEPS) {
        const done = !!(outputs || {})[step.key];
        let glyph;
        let suffix = '';
        if (done) {
            glyph = '✓';
        } else if (isFail) {
            glyph = '✗';
            suffix = ' (échec)';
        } else if (isRunning && !firstPendingMarked) {
            glyph = '⏳';
            suffix = ' (en cours)';
            firstPendingMarked = true;
        } else if (isRunning) {
            glyph = '☐';
            suffix = ' (en attente)';
        } else {
            glyph = '☐';
        }
        lines.push(`${glyph} ${step.label}${suffix}`);
    }
    lines.push('─────────────');
    lines.push('Cliquer pour voir le détail complet.');
    return lines.join('\\n');
}

async function loadTranscriptStatus(fileId, container) {
    try {
        const resp = await fetch(`/api/file/transcript-status/${fileId}`);
        if (!resp.ok) {
            container.innerHTML = '';
            return;
        }
        const data = await resp.json();
        if (!data.available) {
            container.innerHTML = '';
            // re-test dans 30s : la row apparaîtra dès que file-puller a intégré
            setTimeout(() => loadTranscriptStatus(fileId, container), 30000);
            return;
        }
        const status = (data.transcription_status || '').toLowerCase();
        const engine = data.transcription_engine || '';
        const meta = TRANSCRIPT_STATUS_LABELS[status] || { label: status || 'Statut inconnu', polling: false };
        const isInProgress = meta.polling;
        // Met aussi à jour la 4e étape du chemin de fer dans la session.
        updateTranscribeRail(fileId, engine, status);

        const outputs = data.outputs || {};
        const kp = data.key_points_summary || '';
        const title = data.suggested_filename || '';
        // Key points : collapse par défaut (résumé peut faire 1000+ chars).
        // On garde le titre toujours visible, key_points derrière un
        // <details> repliable — SAUF en vue détail (data-persistent-summary=1)
        // où le résumé est toujours visible.
        const persistent = container.getAttribute('data-persistent-summary') === '1';
        const subtitle = (title || kp)
            ? `<div class="transcript-meta">
                ${title && !persistent ? `<div class="transcript-meta-title">${escapeHtml(title)}</div>` : ''}
                ${kp
                    ? (persistent
                        ? `<div class="transcript-meta-persistent-title">Résumé</div><pre class="transcript-meta-keypoints">${escapeHtml(kp)}</pre>`
                        : `<details class="transcript-meta-details"><summary>Résumé</summary><pre class="transcript-meta-keypoints">${escapeHtml(kp)}</pre></details>`)
                    : ''}
              </div>`
            : '';

        // Classification visuelle du bandeau : erreur (rouge ⚠), succès
        // (vert ✓), en cours (bleu pulse) ou neutre (gris).
        const failedStatuses = new Set([
            'failed', 'kevent_failed',
            'mcr_auth_failed', 'mcr_rejected', 'mcr_push_failed',
        ]);
        const successStatuses = new Set([
            'completed', 'kevent_completed', 'kevent_partially_completed',
            'mcr_pushed',
        ]);
        let bannerClass = '';
        let dotClass = 'off';
        let leadIcon = '';
        if (failedStatuses.has(status)) {
            bannerClass = 'transcript-status-error';
            dotClass = 'err';
            leadIcon = '<span class="transcript-status-icon" aria-hidden="true">⚠</span>';
        } else if (successStatuses.has(status)) {
            bannerClass = 'transcript-status-ok';
            dotClass = 'ok';
            leadIcon = '<span class="transcript-status-icon" aria-hidden="true">✓</span>';
        } else if (isInProgress) {
            dotClass = 'on';
        }
        // Diagnostic per-step : on déduit les étapes manquantes des
        // outputs absents (visible uniquement quand la transcription
        // est terminée, partiellement ou non, ou échouée).
        const STEP_INFO = {
            'transcript':              { label: 'Transcription brute',          desc: 'Texte issu du Whisper (faster-whisper). Étape obligatoire pour toutes les autres.' },
            'transcript-tagged':       { label: 'Identification des locuteurs', desc: 'Diarisation pyannote — sépare le texte par interlocuteur. Peut échouer sur les enregistrements très courts ou monolocuteurs.' },
            'transcript-corrected':    { label: 'Correction des sigles',        desc: 'LLM relit le texte avec un glossaire pour corriger les acronymes mal entendus (ex: "EFS" → "EHS" repassé en "EFS").' },
            'transcript-cleaned':      { label: 'Nettoyage hors-sujet',         desc: 'LLM retire les passages parasites (faux départs, bruits ambiants verbalisés).' },
            'transcript-reformulated': { label: 'Discours indirect',            desc: 'LLM reformule au style indirect ("X a dit que...") pour une lecture rapide.' },
            'meeting-cr':              { label: 'Compte-rendu structuré',      desc: 'LLM produit l\\'analyse 5 sections : acteurs, thématiques, décisions, gaps, recommandations.' },
        };
        let stepsDetails = '';
        // On n'affiche le diagnostic que lorsque la transcription est terminée
        // (en cours = pas encore d'outputs) — sinon ça ferait du bruit.
        const showSteps = (status === 'kevent_completed'
                          || status === 'kevent_partially_completed'
                          || status === 'kevent_failed'
                          || status === 'completed' || status === 'failed');
        if (showSteps) {
            const rows = Object.keys(STEP_INFO).map(k => {
                const info = STEP_INFO[k];
                const ok = !!outputs[k];
                const icon = ok ? '✓' : '✗';
                const color = ok ? '#10b981' : '#b91c1c';
                const note = (!ok && k === 'transcript-tagged')
                    ? ' <small style="color:#94a3b8">(pyannote a peut-être eu un problème avec ce signal — voir logs côté admin)</small>'
                    : '';
                return `<div class="status-step">
                    <span style="color:${color};font-weight:700;">${icon}</span>
                    <span class="status-step-label">${escapeHtml(info.label)}</span>
                    <span class="status-step-desc">${escapeHtml(info.desc)}${note}</span>
                </div>`;
            }).join('');
            stepsDetails = `<details class="status-details">
                <summary>Voir le détail des étapes</summary>
                <div class="status-step-list">${rows}</div>
            </details>`;
        }
        const statusBadge = `<div class="transcript-status-line ${bannerClass}">
            ${leadIcon}
            <span class="transcript-status-spinner ${dotClass}"></span>
            <span class="transcript-status-label">${escapeHtml(meta.label)}${engine ? ` <small style="color:#94a3b8">(${escapeHtml(engine)})</small>` : ''}</span>
        </div>${stepsDetails}`;

        // Nouvelle UX downloads : on liste les TYPES de document (pas les
        // formats × types), avec à droite une rangée de boutons-icône, un
        // par format disponible. Pour un néophyte : "Ah je veux le
        // compte-rendu — je clique l'icône Word." Plus de jargon dans le
        // libellé, plus de dropdown 12 lignes.
        let audioOptions = [];
        try {
            const raw = container.getAttribute('data-audio-downloads') || '';
            audioOptions = raw ? JSON.parse(decodeURIComponent(raw)) : [];
        } catch (e) { audioOptions = []; }

        const FMT_ICON = {
            txt:  { svg: ICONS.fmt_txt,  title: 'Texte simple (.txt)' },
            md:   { svg: ICONS.fmt_md,   title: 'Markdown (.md)' },
            docx: { svg: ICONS.fmt_docx, title: 'Word (.docx)' },
            odt:  { svg: ICONS.fmt_odt,  title: 'LibreOffice (.odt)' },
            json: { svg: ICONS.fmt_json, title: 'JSON (.json) — données brutes' },
        };
        const fmtIconHtml = (ext, url) => {
            const fi = FMT_ICON[ext] || { svg: ICONS.fmt_txt, title: `.${ext}` };
            return `<a class="downloads-icon-btn" href="${escapeHtml(url)}"
                       download target="_blank" rel="noopener"
                       title="${escapeHtml(fi.title)}"
                       aria-label="${escapeHtml(fi.title)}">${fi.svg}</a>`;
        };

        // Layout : on met en HAUT les 2 actions courantes (transcription
        // nettoyée + écouter audio interne), puis une section "Autres
        // téléchargements" avec un menu déroulant qui révèle les icônes
        // de format pour le type sélectionné. Plus de bruit visuel.
        const defaultRows = [];
        const otherRows = [];

        const audioDlIcon = (url) =>
            `<a class="downloads-icon-btn" href="${escapeHtml(url)}"
               download target="_blank" rel="noopener"
               title="Télécharger l'audio" aria-label="Télécharger l'audio">${ICONS.fmt_audio}</a>`;
        const audioPlayIcon = (url) =>
            `<a class="downloads-icon-btn downloads-icon-btn-play"
               href="${escapeHtml(url)}" target="_blank" rel="noopener"
               title="Écouter dans le navigateur" aria-label="Écouter">${ICONS.fmt_play}</a>`;

        // Direct (haut de section) = uniquement CR + audio (interne).
        // Le reste passe dans la dropdown "Autres" :
        //   - simple mode (default) : nettoyée + discours indirect seulement
        //   - avancé (toggle ou Alt) : toutes les transcriptions + audios non-interne
        const SIMPLE_OTHER_KINDS = new Set([
            'transcript-cleaned',
            'transcript-reformulated',
        ]);
        const advanced = effectiveAdvancedDl();
        for (const kind of Object.keys(TRANSCRIPT_KIND_LABELS)) {
            if (!outputs[kind]) continue;
            if (!advanced && !SIMPLE_OTHER_KINDS.has(kind)) continue;
            const formats = TRANSCRIPT_KIND_FORMATS[kind] || ['txt'];
            const icons = formats.map((ext) =>
                fmtIconHtml(ext, `/api/file/transcript/${kind}/${ext}/${fileId}`)
            ).join('');
            otherRows.push({ label: TRANSCRIPT_KIND_LABELS[kind], iconsHtml: icons });
        }

        // Audios : interne → accès direct (toujours visible) ; les variantes
        // source/transcodé ne sont accessibles QU'EN MODE AVANCÉ (dropdown).
        for (const a of audioOptions) {
            const isInternal = (a.label || '').toLowerCase().includes('interne');
            if (isInternal && (a.stream || a.dl)) {
                const icons = [];
                if (a.stream) icons.push(audioPlayIcon(a.stream));
                if (a.dl)     icons.push(audioDlIcon(a.dl));
                defaultRows.push({
                    label: "Écouter / Télécharger l'audio (interne)",
                    iconsHtml: icons.join(''),
                });
            } else if (advanced) {
                const icons = [];
                if (a.dl) icons.push(audioDlIcon(a.dl));
                if (a.stream) icons.push(audioPlayIcon(a.stream));
                otherRows.push({ label: a.label, iconsHtml: icons.join('') });
            }
        }

        // Compte-rendu structuré : accès direct (en TÊTE des défauts).
        if (outputs['meeting-cr']) {
            const icons = CR_FORMATS.map((ext) =>
                fmtIconHtml(ext, `/api/file/meeting-cr/${ext}/${fileId}`)
            ).join('');
            defaultRows.unshift({ label: 'Compte-rendu structuré', iconsHtml: icons });
        }

        let dropdownBlock = '';
        if (defaultRows.length > 0 || otherRows.length > 0) {
            const defaultHtml = defaultRows.map(r => `
                <div class="downloads-row">
                    <span class="downloads-row-label">${escapeHtml(r.label)}</span>
                    <span class="downloads-row-icons">${r.iconsHtml}</span>
                </div>`).join('');

            let otherSection = '';
            let pendingOtherSelectId = '';
            if (otherRows.length > 0) {
                // Pas de placeholder : on pré-sélectionne la première entrée.
                // Le HTML des icônes est stocké dans _otherDlIcons (Map JS)
                // pour ne pas dépendre de l'escape HTML d'attribut.
                const optsHtml = otherRows.map((r, i) =>
                    `<option value="${i}"${i === 0 ? ' selected' : ''}>${escapeHtml(r.label)}</option>`
                ).join('');
                const selectId = `other-dl-${fileId}`;
                pendingOtherSelectId = selectId;
                _otherDlIcons.set(selectId, otherRows.map(r => r.iconsHtml));
                otherSection = `
                    <div class="downloads-other-row">
                        <span class="downloads-other-label">Autres :</span>
                        <select class="downloads-other-select fr-select"
                                id="${selectId}"
                                onchange="updateOtherDownload(this)">
                            ${optsHtml}
                        </select>
                        <span class="downloads-row-icons downloads-other-icons"
                              data-other-icons-for="${selectId}"></span>
                    </div>`;
            }
            dropdownBlock = `<div class="downloads-block">${defaultHtml}${otherSection}</div>`;
        }

        container.innerHTML = `${statusBadge}${subtitle}${dropdownBlock}`;
        // Peuple immédiatement les icônes du select "Autres téléchargements"
        // pour la première entrée sélectionnée (sinon la zone reste vide
        // jusqu'au premier change).
        container.querySelectorAll('.downloads-other-select').forEach((sel) => {
            updateOtherDownload(sel);
        });
        // Restaure la sélection du dropdown si l'utilisateur avait avancé
        // (mémorisée par loadSessions dans window._savedDlSelections).
        try {
            const saved = window._savedDlSelections && window._savedDlSelections.get(fileId);
            if (Number.isInteger(saved)) {
                const sel = container.querySelector('.downloads-select');
                if (sel && saved >= 0 && saved < sel.options.length) {
                    sel.selectedIndex = saved;
                    updateDownloadButtons(sel);
                }
            }
        } catch (e) {}
        // Met à jour le titre cliquable de la ligne compacte avec le
        // suggested_filename (généré par l'IA) si disponible — plus
        // parlant que le filename technique poemes013_xxx.mp3. Le
        // rollover affiche le filename d'origine pour traçabilité.
        // Met aussi le tooltip de la pastille statut compacte avec le
        // label complet (ex: "Pipeline Kevent partiel...").
        try {
            if (title) {
                const link = document.querySelector(
                    `.file-row-title[data-file-id="${fileId}"]`
                );
                if (link) {
                    link.textContent = title;
                }
                // En vue détail, pré-remplit l'input éditable du titre
                // une seule fois (puis on n'overwrite plus, l'utilisateur
                // peut être en train de saisir une nouvelle valeur).
                const titleInput = document.querySelector(
                    `[data-detail-title-for="${fileId}"]`
                );
                if (titleInput && !titleInput.dataset.prefilled) {
                    titleInput.value = title;
                    titleInput.dataset.prefilled = '1';
                    titleInput.dataset.originalTitle = title;
                }
            }
            if (container.getAttribute('data-compact') === '1') {
                // Met à jour le dot caractère unicode "●" inline dans la
                // file-row (aligné naturellement avec le titre). La couleur
                // dépend du statut via la classe file-row-dot-<status>,
                // l'animation pulse aussi (les classes in-progress portent
                // l'animation CSS — cf. @keyframes filerowDotPulse).
                const dot = document.querySelector(`[data-file-dot="${fileId}"]`);
                if (dot) {
                    dot.className = `file-row-dot file-row-dot-${status}`;
                    const friendly = (TRANSCRIPT_STATUS_LABELS[status] || {}).label || status;
                    dot.title = `Étape en cours : ${friendly}${engine ? ' (' + engine + ')' : ''}`;
                }
                // Bouton (i) : tooltip multi-ligne avec checklist par étape
                // (☐/✓/✗). Pulse + bordure bleue si le pipeline tourne.
                const infoBtn = document.querySelector(`[data-file-info-btn="${fileId}"]`);
                if (infoBtn) {
                    infoBtn.title = _buildInfoTooltip(status, engine, outputs, meta);
                    infoBtn.classList.toggle('is-in-progress', isInProgress);
                }
                // Mémorise les infos pour le modal (status raw, engine,
                // outputs map). On ne re-fetch pas quand l'utilisateur
                // clique sur (i), on lit ce cache.
                window._fileInfoCache = window._fileInfoCache || {};
                window._fileInfoCache[fileId] = {
                    status, engine, label: meta.label,
                    outputs: outputs, title, kp,
                    language: data.transcription_language,
                };
                // Hint file d'attente : assure que le poll global tourne
                // tant qu'il existe au moins un fichier non-terminal (liste
                // ou détail). Le poll global ensureQueueHintPolling est
                // idempotent + auto-stop quand plus aucun widget dispo.
                // Pollabilité : on marque le widget queue-hint comme pollable
                // tant que le statut est non-terminal. _pollQueueHintAll ne
                // touchera plus les widgets non-pollables et videra leur texte
                // — ça évite "⏳ Réservation de la file…" qui restait sur les
                // fichiers passés à kevent_failed/_completed/_partially.
                const isPollable = !_TERMINAL_TS.has(status);
                document.querySelectorAll(
                    `[data-queue-hint-for="${fileId}"]`
                ).forEach((el) => {
                    if (isPollable) {
                        el.setAttribute('data-pollable', '1');
                        if (data.kevent_job_id) {
                            el.setAttribute('data-queue-job-id', data.kevent_job_id);
                        }
                    } else {
                        el.removeAttribute('data-pollable');
                        el.removeAttribute('data-queue-job-id');
                        el.textContent = '';
                    }
                });
                if (isPollable) ensureQueueHintPolling();
                // Zone résumé : on n'affiche que les key_points (pas le
                // label statut "Pipeline Kevent partiel..." qui est déjà
                // sur la pastille via tooltip).
                const expandedSummary = document.querySelector(
                    `[data-expanded-summary-for="${fileId}"]`
                );
                if (expandedSummary) {
                    expandedSummary.innerHTML = kp
                        ? `<pre class="transcript-meta-keypoints">${escapeHtml(kp)}</pre>`
                        : `<div class="file-row-expanded-empty">Pas de résumé disponible.</div>`;
                }
            }
        } catch (e) {}

        // Re-poll automatique tant qu'on est en cours, pour ne pas obliger
        // l'utilisateur à recharger la page pour voir la transcription apparaître.
        if (isInProgress) {
            setTimeout(() => loadTranscriptStatus(fileId, container), 15000);
        }
    } catch (e) {
        container.innerHTML = '';
    }
}

// Toast léger en bas-droite : disparaît après 4s.
function showToast(message, kind) {
    const t = document.createElement('div');
    t.textContent = message;
    t.className = `toast toast-${kind || 'info'}`;
    document.body.appendChild(t);
    requestAnimationFrame(() => t.classList.add('toast-show'));
    setTimeout(() => {
        t.classList.remove('toast-show');
        setTimeout(() => t.remove(), 250);
    }, 4000);
}

async function loadNormalizationImpact(fileId) {
    if (impactLoading.has(fileId)) return;
    impactLoading.add(fileId);
    loadSessions();
    try {
        const resp = await fetch(`/api/file/normalization-impact/${fileId}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur analyse');
        const msg =
            `Avant LUFS ${data.source.i}, Après ${data.normalized.i}, ` +
            `ΔLUFS ${data.delta.i}. TP: ${data.source.tp} -> ${data.normalized.tp}. ` +
            `LRA: ${data.source.lra} -> ${data.normalized.lra}. ` +
            `Amélioration cible -16 LUFS: ${data.improvement_to_target_lufs}.`;
        impactCache[fileId] = {
            text: msg,
            at: new Date().toLocaleString('fr-FR'),
        };
        showToast('Impact de la normalisation calculé.', 'success');
        // Si le modal est ouvert, met aussi à jour son contenu impact
        const modalImpact = document.getElementById(`modal-impact-${fileId}`);
        if (modalImpact) modalImpact.textContent = msg;
    } catch (e) {
        const msg = `Erreur: ${e.message}`;
        impactCache[fileId] = {
            text: msg,
            at: new Date().toLocaleString('fr-FR'),
        };
        showToast(`Impact normalisation : ${e.message}`, 'error');
    } finally {
        impactLoading.delete(fileId);
        loadSessions();
    }
}

// Tabs : navigation entre Mes appareils / Mes transferts / Nouveau code.
// L'onglet par défaut est choisi par le 1er loadDevices selon la présence
// d'un device actif. Persiste le choix dans sessionStorage pour ne pas
// switcher au refresh.
let _tabsInitialised = false;
function activateTab(tabName) {
    document.querySelectorAll('.tab-btn').forEach((b) => {
        const on = b.getAttribute('data-tab') === tabName;
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.tab-pane').forEach((p) => {
        p.classList.toggle('is-active', p.getAttribute('data-tab') === tabName);
    });
    try { sessionStorage.setItem('mydevices-active-tab', tabName); } catch (e) {}
}
function setupTabs() {
    document.querySelectorAll('.tab-btn').forEach((b) => {
        b.addEventListener('click', () => {
            const target = b.getAttribute('data-tab');
            // Clic sur "Mes réunions IA" = retour à la vue liste (même si
            // on est déjà sur l'onglet transfers en vue détail).
            if (target === 'transfers') {
                showFilesList();
            }
            if (target === 'trash') {
                loadTrash();
            }
            activateTab(target);
        });
    });
}

async function loadTrash() {
    const container = document.getElementById('trash-list');
    if (!container) return;
    try {
        const resp = await fetch('/api/my-trash');
        const data = await resp.json();
        const files = data.files || [];
        const sessions = data.sessions || [];
        if (files.length === 0 && sessions.length === 0) {
            container.innerHTML = `<p style="color:#64748b;font-size:0.85rem;">
                La corbeille est vide. Les éléments supprimés y restent ${data.retention_days || 30} jours avant suppression définitive.
            </p>`;
            return;
        }
        const sessionsHtml = sessions.map(s => `
            <div class="trash-item">
                <span class="trash-item-type">Session</span>
                <span class="trash-item-name"><strong>${escapeHtml(s.simple_code)}</strong> · ${s.files_count} fichier(s)</span>
                <span class="trash-item-meta">reste ${s.days_left} j avant purge</span>
                <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="restoreSession('${s.simple_code}')">Restaurer</button>
            </div>
        `).join('');
        const filesHtml = files.map(f => `
            <div class="trash-item">
                <span class="trash-item-type">Fichier</span>
                <span class="trash-item-name">${escapeHtml(f.original_filename)} <small style="color:#94a3b8;">(${escapeHtml(f.simple_code || '?')})</small></span>
                <span class="trash-item-meta">reste ${f.days_left} j avant purge</span>
                <button class="btn-primary fr-btn fr-btn--sm fr-btn--secondary"
                        onclick="restoreFile('${f.id}')">Restaurer</button>
                <button class="btn-primary btn-danger-mini fr-btn fr-btn--sm fr-btn--tertiary-no-outline"
                        onclick="deleteFilePermanently('${f.id}', '${escapeHtml(f.original_filename).replace(/'/g, '&#39;')}')">
                    Supprimer définitivement
                </button>
            </div>
        `).join('');
        container.innerHTML = sessionsHtml + filesHtml;
    } catch (e) {
        container.innerHTML = `<p style="color:#b91c1c;font-size:0.85rem;">Erreur chargement corbeille.</p>`;
    }
}

async function restoreFile(fileId) {
    try {
        const r = await fetch(`/api/file/${fileId}/restore`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'restore_failed');
        showToast('Fichier restauré.', 'success');
        loadTrash();
        loadSessions({ force: true });
    } catch (e) { showToast('Restauration échouée.', 'error'); }
}

async function restoreSession(simpleCode) {
    try {
        const r = await fetch(`/api/my-sessions/${simpleCode}/restore`, { method: 'POST' });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'restore_failed');
        showToast('Session restaurée.', 'success');
        loadTrash();
        loadSessions({ force: true });
    } catch (e) { showToast('Restauration échouée.', 'error'); }
}

async function deleteFilePermanently(fileId, filenameRaw) {
    const filename = (filenameRaw || '').replace(/&#39;/g, "'");
    if (!confirm(`Supprimer définitivement « ${filename} » ?\\n\\nLe fichier sera retiré de S3 et de la base. Cette action est irréversible.`)) return;
    try {
        const r = await fetch(`/api/file/${fileId}/permanently`, { method: 'DELETE' });
        const d = await r.json();
        if (!r.ok || !d.ok) throw new Error(d.error || 'delete_failed');
        showToast('Fichier supprimé définitivement.', 'success');
        loadTrash();
    } catch (e) { showToast('Suppression définitive échouée.', 'error'); }
}
function pickDefaultTab(hasActiveDevice) {
    if (_tabsInitialised) return;
    _tabsInitialised = true;
    let target = null;
    try { target = sessionStorage.getItem('mydevices-active-tab'); } catch (e) {}
    if (!target) {
        // Sans device : on guide direct vers le formulaire d'enrôlement.
        // Avec device : vue principale = transferts/analyses.
        target = hasActiveDevice ? 'transfers' : 'generate';
    }
    activateTab(target);
}

setupTabs();
updateDeviceFilterButton();
updateAdvancedToggleUi();
// Feedback visuel sur clic d'une icône de téléchargement : flash + scale.
// Délégation globale — fonctionne pour les boutons re-rendus par
// loadTranscriptStatus sans re-bind à chaque refresh.
document.addEventListener('click', (ev) => {
    const btn = ev.target.closest && ev.target.closest('.downloads-icon-btn');
    if (!btn) return;
    btn.classList.add('is-clicked');
    setTimeout(() => btn.classList.remove('is-clicked'), 450);
});
// Charge initial : devices puis sessions. Pas d'auto-refresh setInterval —
// le user peut Rafraîchir manuellement via le bouton dédié dans le header
// de l'onglet, ou la transcription qui poll elle-même (loadTranscriptStatus
// re-fire dans 15-30s tant qu'isInProgress).
loadDevices().then(() => loadSessions({ force: true })).catch(() => loadSessions({ force: true }));
</script>
<script type="module" src="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14.2/dist/dsfr/dsfr.module.min.js"></script>
<script nomodule src="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14.2/dist/dsfr/dsfr.nomodule.min.js"></script>
</body>
</html>
"""


# ─── Init & Run ─────────────────────────────────────────────

def create_app():
    global SessionLocal
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    SessionLocal = create_session_factory(db_cfg)
    return app


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port_value = os.getenv("CODE_GENERATOR_BIND_PORT") or os.getenv("CODE_GENERATOR_PORT", "8080")
    if isinstance(port_value, str) and port_value.startswith("tcp://"):
        port_value = os.getenv("CODE_GENERATOR_BIND_PORT", "8080")
    port = int(port_value)
    application.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "development")
