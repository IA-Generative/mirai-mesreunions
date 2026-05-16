"""
Code Generator Service
======================
Authenticated interface (OIDC/Keycloak) for generating QR codes
and simple codes that link to the upload portal.

CHANGEMENT CLÉ : les tokens (simple_code + qr_token) sont générés
côté INTERNE par le device-token-authority. Ce service ne fait que relayer
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
from flask import Flask, redirect, url_for, session, render_template, render_template_string, jsonify, request, abort, send_file
from authlib.integrations.flask_client import OAuth

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    OIDCConfig, load_ext_db, CODE_TTL_MINUTES, CODE_TTL_MAX_MINUTES,
    MAX_UPLOADS_PER_SESSION, SECRET_KEY, UPLOAD_PORTAL_BASE_URL, load_s3_upload, load_s3_processed, load_s3_internal,
    UPLOAD_STATUS_VIEW_TTL_MINUTES, TOKEN_ISSUER_API_URL, INTERNAL_API_TOKEN,
    OIDC_OFFLINE_ACCESS, DEVICE_TOKEN_RETENTION_HOURS,
    UPLOAD_MAX_FILE_SIZE_MB, ALLOWED_AUDIO_EXTENSIONS, RabbitMQConfig,
    DRIVE_BASE_URL, OIDC_TOKEN_ENDPOINT,
    LITELLM_BASE_URL, LITELLM_API_KEY, LLM_MODEL_MEDIUM, LLM_HTTP_TIMEOUT_SECONDS,
)
from libs.shared.app.oidc_refresh_store import store_refresh_token, fetch_ciphertext
from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
from libs.shared.app.models import (
    ExternalBase, UploadSession, UploadedFile, SessionStatus, UploadStatus, UploadTokenOption
)
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.s3_helper import download_fileobj, delete_object, object_exists
from libs.shared.app.upload_helpers import (
    is_allowed_audio_filename, build_stored_filename, store_audio_to_s3, publish_av_scan_message,
)

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
rabbit_cfg = RabbitMQConfig()
# Limite alignée sur mobile-upload-pwa pour rester cohérent quand l'utilisateur
# uploade directement depuis mydevices (POST /api/my-upload).
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024
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


def get_mobile_upload_pwa_base_url() -> str:
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
    Appelle le device-token-authority en zone INTERNE pour obtenir un (simple_code, qr_token).
    Le mydevices-web ne génère plus jamais de token lui-même.
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


def request_internal_ingester_api(path: str, *, json_body=None, params=None, method: str = "POST", timeout: int = 10) -> dict | None:
    """Call internal-ingester's internal API. Returns the parsed JSON or None on 404.

    Used by the user-facing transcript download endpoints to fetch the
    user_audio_files row that lives in postgres-internal. Auth =
    INTERNAL_API_TOKEN (same bearer internal-ingester uses for /api/v1/pull).
    """
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL", "http://internal-ingester:8090").rstrip("/")
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
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise req.HTTPError(f"internal-ingester {path} → {resp.status_code}: {resp.text[:200]}",
                            response=resp)
    return resp.json()


def request_internal_device_api(method: str, path: str, *, json_body=None, timeout: int = 10, params=None) -> dict:
    base = os.getenv("TOKEN_ISSUER_INTERNAL_BASE_URL", "http://device-token-authority:8091").rstrip("/")
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

    Préparations + meetings (zone interne) : on relaie vers device-token-authority
    /api/v1/preparations/purge et /api/v1/meetings/purge avec le même
    seuil. Best-effort — un échec réseau ne casse pas le balayage fichiers.
    """
    threshold = datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)
    sessions_purged = 0
    files_purged = 0
    objects_deleted = 0
    preparations_purged = 0
    meetings_purged = 0
    try:
        result = request_internal_device_api(
            "POST", "/api/v1/preparations/purge",
            json_body={"user_sub": user_sub, "older_than_days": TRASH_RETENTION_DAYS},
        )
        preparations_purged = int(result.get("purged") or 0)
    except Exception:
        logger.debug(
            "trash purge: preparation purge relay failed for user=%s",
            user_sub, exc_info=True,
        )
    try:
        result = request_internal_device_api(
            "POST", "/api/v1/meetings/purge",
            json_body={"user_sub": user_sub, "older_than_days": TRASH_RETENTION_DAYS},
        )
        meetings_purged = int(result.get("purged") or 0)
    except Exception:
        logger.debug(
            "trash purge: meeting purge relay failed for user=%s",
            user_sub, exc_info=True,
        )

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
    if sessions_purged or files_purged or preparations_purged or meetings_purged:
        logger.info(
            "trash purge: user=%s sessions=%s files=%s s3_objects=%s preparations=%s meetings=%s",
            user_sub, sessions_purged, files_purged, objects_deleted,
            preparations_purged, meetings_purged,
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
    """Fetch the kevent / mcr / stub outputs for a file from internal-ingester.

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
        return request_internal_ingester_api(
            "/api/v1/audio/lookup",
            json_body={
                "user_sub": session_obj.user_sub,
                "simple_code": session_obj.simple_code,
                "stored_filename": file_obj.transcoded_filename,
            },
        )
    except req.RequestException:
        logger.exception("internal-ingester lookup failed for file_id=%s", file_obj.id)
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
    return jsonify({"status": "ok", "service": "mydevices-web"}), 200


@app.route("/")
@require_auth
def index():
    user = get_current_user()
    return render_template(
        "index.html",
        user=user,
        short_ttl_enabled=ALLOW_SHORT_QR_TTL_SECONDS_TEST,
        device_retention_days=max(1, DEVICE_TOKEN_RETENTION_HOURS // 24),
        allowed_audio_extensions=",".join(ALLOWED_AUDIO_EXTENSIONS),
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
    Demande un token au device-token-authority (zone interne), puis stocke
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

    # ── Appel au device-token-authority INTERNE ──
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

    upload_url = f"{get_mobile_upload_pwa_base_url()}/upload/{qr_token}"

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
    upload_url = f"{get_mobile_upload_pwa_base_url()}/upload/{qr_token}"
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

        # Bulk-fetch des overrides "date de réunion" (zone interne). Un seul
        # appel → internal-ingester renvoie uniquement les rows non-NULL. Map indexée
        # par (simple_code, original_filename) pour l'enrichissement par fichier.
        meeting_dt_overrides: dict[tuple[str, str], str] = {}
        try:
            bulk = request_internal_ingester_api(
                "/api/v1/audio/meeting-datetimes",
                method="GET",
                params={"user_sub": user["sub"]},
            )
            if isinstance(bulk, dict):
                for it in (bulk.get("items") or []):
                    code = (it.get("simple_code") or "").strip()
                    name = (it.get("original_filename") or "").strip()
                    dt = it.get("meeting_datetime")
                    if code and name and dt:
                        meeting_dt_overrides[(code, name)] = dt
        except Exception:
            logger.debug("meeting-datetimes bulk fetch failed (non-fatal)", exc_info=True)

        # On récupère la liste des devices encore actifs pour cet utilisateur
        # (device-token-authority GET /api/v1/devices). Permet à _compute_lifecycle_state
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
            # Best-effort : si device-token-authority ne répond pas, on retombe sur le
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

                override_dt = meeting_dt_overrides.get((s.simple_code, f.original_filename))
                uploads.append({
                    "id": str(f.id),
                    "original_filename": f.original_filename,
                    "status": f.status.value,
                    "status_message": f.status_message,
                    "audio_quality_score": f.audio_quality_score,
                    "audio_duration_seconds": f.audio_duration_seconds,
                    "created_at": f.created_at.isoformat(),
                    "updated_at": f.updated_at.isoformat() if f.updated_at else None,
                    "meeting_datetime": override_dt,
                    "meeting_datetime_overridden": override_dt is not None,
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
            is_local = (s.simple_code or "").startswith(_LOCAL_UPLOAD_SIMPLE_CODE_PREFIX)
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
                "is_local_upload": is_local,
                "device_label": _LOCAL_UPLOAD_DEVICE_LABEL if is_local else None,
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
# Each route serves one output format. internal-ingester is queried once per call —
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

    Returns 404 if the file isn't owned by the user, 503 if internal-ingester is
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
            "meeting_datetime": audio.get("meeting_datetime"),
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
    """Proxy live vers internal-ingester /api/v1/queue-status (qui parle au gateway).

    Accepte 2 modes d'auth :
      - session OIDC (utilisateur connecté à mydevices)
      - bearer INTERNAL_API_TOKEN (cross-cluster depuis mobile-upload-pwa)

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
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL", "http://internal-ingester:8090").rstrip("/")
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
        logger.warning("queue-status proxy to internal-ingester failed", exc_info=True)
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

    Body: ``{"title": "..."}``. Persiste via device-token-authority
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


# ─── Upload local (sans QR / sans device-token) ──────────────────────────────
# Marqueur en tête de simple_code identifiant les sessions créées par
# /api/my-upload (utilisateur authentifié OIDC, upload depuis le navigateur).
# La présence de ce préfixe sert à : (a) afficher "Upload local" comme device,
# (b) éviter qu'une session locale soit confondue avec une session QR
# multi-device.
_LOCAL_UPLOAD_SIMPLE_CODE_PREFIX = "L-"
_LOCAL_UPLOAD_MAX_PER_SESSION = 9999
_LOCAL_UPLOAD_DEVICE_LABEL = "Upload local"


def _get_or_create_local_upload_session(db, user) -> UploadSession:
    """Trouve une session 'upload local' active réutilisable, sinon en crée
    une neuve. On veut une seule virtual-session par utilisateur tant qu'elle
    n'a pas saturé son quota — ça évite de spammer la table à chaque batch.
    """
    sess = (
        db.query(UploadSession)
        .filter(
            UploadSession.user_sub == user["sub"],
            UploadSession.simple_code.like(f"{_LOCAL_UPLOAD_SIMPLE_CODE_PREFIX}%"),
            UploadSession.status == SessionStatus.ACTIVE,
            UploadSession.trashed_at.is_(None),
            UploadSession.upload_count < UploadSession.max_uploads,
        )
        .order_by(UploadSession.created_at.desc())
        .first()
    )
    if sess:
        return sess
    # Création : simple_code "L-XXXXXXXX" (10 chars, respecte VARCHAR(10)).
    # qr_token : random 64-hex pour respecter l'index UNIQUE. expires_at très
    # loin dans le futur — l'endpoint /api/my-upload ne consulte pas cette
    # date (auth OIDC, pas device-token).
    suffix = secrets.token_hex(4).upper()  # 8 chars hex
    simple_code = f"{_LOCAL_UPLOAD_SIMPLE_CODE_PREFIX}{suffix}"
    new = UploadSession(
        id=uuid4(),
        user_sub=user.get("sub"),
        user_email=user.get("email"),
        user_display_name=user.get("name") or user.get("preferred_username"),
        simple_code=simple_code,
        qr_token=secrets.token_hex(32),
        status=SessionStatus.ACTIVE,
        max_uploads=_LOCAL_UPLOAD_MAX_PER_SESSION,
        upload_count=0,
        ttl_minutes=0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=365 * 50),
    )
    db.add(new)
    db.flush()  # garantit que l'ID est dispo + détecte les collisions tôt
    return new


@app.route("/api/my-upload", methods=["POST"])
@require_auth
def api_my_upload():
    """Upload local d'un fichier audio depuis mydevices (sans QR / device-token).

    L'utilisateur est déjà authentifié OIDC, donc on peut court-circuiter le
    flow PWA mobile. Le fichier rejoint le pipeline existant
    (AV → transcode → transfer → kevent) via une virtual-session marquée
    ``L-XXXXXXXX``. Côté UI le device sera affiché comme "Upload local".

    Body multipart: ``file=<binary>``. Réponse identique à l'mobile-upload-pwa :
    ``{file_id, filename, status, remaining}``.
    """
    user = get_current_user()
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier sélectionné."}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Nom de fichier vide."}), 400
    if not is_allowed_audio_filename(file.filename):
        return jsonify({
            "error": f"Format non supporté. Formats acceptés : {', '.join(ALLOWED_AUDIO_EXTENSIONS)}"
        }), 400

    file_data = file.read()
    file_size = len(file_data)
    if file_size == 0:
        return jsonify({"error": "Fichier vide."}), 400

    db = SessionLocal()
    try:
        session_obj = _get_or_create_local_upload_session(db, user)
        stored_name = build_stored_filename(session_obj.simple_code, file.filename)
        try:
            store_audio_to_s3(s3_upload_cfg, stored_name, file_data, file.content_type)
        except Exception:
            db.rollback()
            logger.exception("S3 upload failed for local upload")
            return jsonify({"error": "Erreur lors de l'upload. Réessayez."}), 500

        uploaded_file = UploadedFile(
            id=uuid4(),
            session_id=session_obj.id,
            original_filename=file.filename,
            stored_filename=stored_name,
            file_size_bytes=file_size,
            mime_type=file.content_type,
            status=UploadStatus.PENDING,
            status_message="Fichier reçu (upload local), en attente d'analyse antivirale...",
        )
        db.add(uploaded_file)
        session_obj.upload_count += 1
        db.commit()
        file_id = str(uploaded_file.id)
        simple_code = session_obj.simple_code
        user_sub = session_obj.user_sub
        user_email = session_obj.user_email
        session_id_str = str(session_obj.id)
        remaining = max(0, session_obj.max_uploads - session_obj.upload_count)
    finally:
        db.close()

    try:
        publish_av_scan_message(
            rabbit_cfg,
            file_id=file_id,
            session_id=session_id_str,
            stored_filename=stored_name,
            original_filename=file.filename,
            simple_code=simple_code,
            user_sub=user_sub,
            user_email=user_email,
        )
    except Exception:
        logger.exception("Failed to publish to QUEUE_AV_SCAN for local upload")

    return jsonify({
        "file_id": file_id,
        "filename": file.filename,
        "status": "pending",
        "remaining": remaining,
    })


@app.route("/api/file/<file_id>/meeting-datetime", methods=["PATCH"])
@require_auth
def api_set_meeting_datetime(file_id):
    """Surcharge la date/heure de réunion (zone interne).

    Body: ``{"meeting_datetime": "ISO 8601" | null}``. Persiste via device-token-authority
    ``POST /api/v1/files/by-session/meeting-datetime`` (matching identique au
    rename : user_sub + simple_code + original_filename). ``null`` efface
    l'override.
    """
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    if "meeting_datetime" not in payload:
        return jsonify({"error": "meeting_datetime field required (string or null)"}), 400
    raw_dt = payload.get("meeting_datetime")
    if raw_dt is not None and (not isinstance(raw_dt, str) or not raw_dt.strip()):
        return jsonify({"error": "meeting_datetime must be ISO 8601 string or null"}), 400

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
                "/api/v1/files/by-session/meeting-datetime",
                json_body={
                    "user_sub": user["sub"],
                    "simple_code": session_obj.simple_code,
                    "original_filename": file_obj.original_filename,
                    "meeting_datetime": raw_dt.strip() if isinstance(raw_dt, str) else None,
                },
            )
        except req.HTTPError as err:
            if err.response is not None:
                try:
                    body = err.response.json()
                except Exception:
                    body = {"error": "internal_api_error"}
                return jsonify({"error": body.get("error", "meeting_datetime_failed")}), err.response.status_code
            return jsonify({"error": "meeting_datetime_failed"}), 502
        return jsonify({
            "ok": True,
            "meeting_datetime": data.get("meeting_datetime"),
            "meeting_datetime_overridden": data.get("meeting_datetime") is not None,
        })
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

        # Préparations + meetings en corbeille — relayés depuis
        # device-token-authority (zone interne). Best-effort : un échec ne casse pas
        # le rendu des fichiers/sessions.
        def _enrich_with_days_left(items, title_keys):
            out = []
            for it in items:
                trashed_iso = it.get("trashed_at")
                days_left = None
                if trashed_iso:
                    try:
                        ts = datetime.fromisoformat(trashed_iso.replace("Z", "+00:00"))
                        days_left = max(
                            0,
                            TRASH_RETENTION_DAYS - (now - ts.astimezone(timezone.utc)).days,
                        )
                    except Exception:
                        days_left = None
                title = None
                for k in title_keys:
                    title = it.get(k)
                    if title:
                        break
                out.append({
                    "id": it.get("id"),
                    "title": title or "(sans titre)",
                    "trashed_at": trashed_iso,
                    "days_left": days_left,
                })
            return out

        preparations_list = []
        try:
            data = request_internal_device_api(
                "GET", "/api/v1/preparations",
                params={"user_sub": user["sub"], "trashed": "true", "limit": 200},
            )
            preparations_list = _enrich_with_days_left(
                data.get("preparations", []), ("title", "subject"),
            )
        except Exception:
            logger.debug("trash listing: preparation relay failed", exc_info=True)

        meetings_list = []
        try:
            data = request_internal_device_api(
                "GET", "/api/v1/meetings",
                params={"user_sub": user["sub"], "trashed": "true", "limit": 200},
            )
            meetings_list = _enrich_with_days_left(
                data.get("meetings", []), ("title", "summary"),
            )
        except Exception:
            logger.debug("trash listing: meeting relay failed", exc_info=True)

        return jsonify({
            "files": files_list,
            "sessions": sessions_list,
            "preparations": preparations_list,
            "meetings": meetings_list,
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
    purge définitive (DB + S3 + cleanup interne via device-token-authority) est faite
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

        # Chemin rapide : si audio-normalizer a déjà persisté les mesures
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


# ─── Meeting-prep wizard (page routes uniquement) ───────────
#
# PR3 : les endpoints API ``/api/meeting-prep/*`` ont été remplacés par
# les blueprints ``preparations_bp`` (``/api/preparations/*``) et
# ``meetings_bp`` (``/api/meetings/*``) — voir ``app/modules/``. Les routes
# qui suivent ne servent plus que les pages HTML du wizard standalone.

# Import lazy conservé : le wizard standalone (PREP_BRIEF_TEMPLATE) peut
# encore appeler des constantes du module si nécessaire.
from app import meeting_prep as _meeting_prep  # noqa: E402,F401



# Endpoints API meeting-prep retirés en PR3 — remplacés par les
# blueprints ``app.modules.preparations`` (``/api/preparations/*``) et
# ``app.modules.meetings`` (``/api/meetings/*``).


@app.route("/meeting-prep")
@require_auth
def meeting_prep_page():
    """Deep-link historique → onglet « Préparation de réunion » de mydevices."""
    return redirect("/?tab=brief", code=302)


@app.route("/meeting-prep/new")
@require_auth
def meeting_prep_new_page():
    """Page wizard plein écran — création d'une nouvelle préparation."""
    user = get_current_user()
    return render_template_string(
        PREP_BRIEF_TEMPLATE, user=user, drive_base_url=(DRIVE_BASE_URL or "")
    )


PREP_BRIEF_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MIrAI - Préparer un brief de réunion</title>
  <link rel="icon" type="image/png" sizes="192x192" href="/static/icons/pwa-icon-192.png">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@gouvfr/dsfr@1.14.2/dist/dsfr/dsfr.min.css">
  <style>
    * { box-sizing: border-box; }
    body { background:#f6f6f6; color:#161616; margin:0; min-height:100vh; }
    .page { max-width: 760px; margin:0 auto; padding:1.5rem 1rem 3rem; }
    .card { background:#fff; border:1px solid #e5e5e5; border-radius:0.5rem;
            padding:1.25rem; margin-bottom:1rem; }
    h1 { font-size: 1.3rem; color:#1a1a2e; margin:0 0 0.4rem; }
    .banner { background:#eef3fb; border:1px solid #cfd9eb; padding:0.85rem 1rem;
              border-radius:0.5rem; font-size:0.9rem; color:#1a2640;
              margin-bottom: 1rem; line-height: 1.45; }
    .banner strong { display:block; margin-bottom:0.2rem; font-size:0.95rem; }
    .question { margin-bottom: 1.15rem; }
    .question > label { display:block; font-size:0.9rem; font-weight:600;
                        color:#1a1a2e; margin-bottom:0.35rem; }
    .question .hint { font-size:0.78rem; color:#64748b; margin-bottom:0.4rem; }
    input[type=text], input[type=number], textarea {
      width:100%; padding:0.55rem 0.7rem; border:1px solid #d0d5dd;
      border-radius:6px; font-size:0.92rem; background:#fff; color:#161616;
      font-family: inherit;
    }
    input:focus, textarea:focus { outline:2px solid #6a6af4; outline-offset:0; }
    .chips { display:flex; flex-wrap:wrap; gap:0.4rem; margin-top:0.4rem; }
    .chip { display:inline-block; padding:0.25rem 0.6rem; background:#f1f3f9;
            border:1px solid #d8def0; border-radius:999px; font-size:0.78rem;
            color:#33396b; cursor:pointer; user-select:none; }
    .chip:hover { background:#e3e8f6; }
    .focus-grid { display:grid; grid-template-columns: 1fr 1fr; gap:0.35rem 1rem;
                  margin-top:0.2rem; }
    .focus-grid label { font-size:0.85rem; font-weight:400; color:#1a1a2e;
                        display:flex; align-items:center; gap:0.45rem;
                        cursor:pointer; }
    .actions { display:flex; justify-content:space-between; align-items:center;
               margin-top:0.5rem; }
    .btn { padding:0.55rem 0.95rem; border-radius:6px; border:none;
           font-size:0.9rem; font-weight:600; cursor:pointer; }
    .btn-primary { background:#000091; color:#fff; }
    /* Meeting-prep v2 §5b : explicite la couleur de texte au hover/focus
       du bouton primaire pour éviter la régression d'inversion de contraste
       (label blanc sur fond gris clair hérité du CSS commun du portail). */
    .btn-primary:hover, .btn-primary:focus-visible, .btn-primary:active {
      background:#1212a0; color:#fff;
    }
    .btn-primary:disabled { background:#94a3b8; cursor:not-allowed; }
    .btn-secondary { background:#fff; color:#000091; border:1px solid #000091; }
    .btn-secondary:hover, .btn-secondary:focus-visible {
      background:#eef3fb; color:#000091;
    }
    .status { margin-top:0.8rem; padding:0.55rem 0.75rem; border-radius:6px;
              font-size:0.85rem; display:none; }
    .status.info { background:#eef3fb; color:#1a2640; display:block; }
    .status.err { background:#fbe5e5; color:#7a1f1f; display:block; }
    .brief-output { white-space: normal; }
    .brief-section { margin-bottom: 0.9rem; }
    .brief-section h3 { font-size:0.95rem; color:#1a1a2e; margin:0 0 0.35rem; }
    .brief-section ul { margin: 0; padding-left: 1.2rem; }
    .brief-section li { font-size: 0.88rem; margin-bottom: 0.2rem; }
    .doc-list { font-size: 0.78rem; color: #475569; margin-top: 0.5rem; }
    .doc-list li.ingested { color: #1a4d2b; }
    .doc-list li.skipped, .doc-list li.error { color: #7a4a1f; }
    .header-bar { display:flex; justify-content:space-between; align-items:baseline;
                  margin-bottom:0.8rem; }
    .header-bar .who { font-size:0.78rem; color:#475569; }
    .header-bar a { font-size:0.85rem; color:#000091; }
  </style>
</head>
<body>
<main class="page">
  <div class="header-bar">
    <span class="who">{{ user.name or user.email }}</span>
    <span><a href="/">Retour à l'accueil</a> · <a href="/logout">Déconnexion</a></span>
  </div>

  <div class="banner">
    <strong>📋 Préparer votre brief de réunion</strong>
    L'IA lit vos documents de prép (Drive) et produit un brief personnalisé +
    un glossaire qui servira à mieux retranscrire l'audio de la réunion.
    4 questions rapides pour adapter le brief à votre besoin.
  </div>

  <form id="prep-form" class="card" autocomplete="off">
    <h1>Brief de réunion</h1>

    <!-- Meeting-prep v2 §7 : badge "Suite de : <titre>" si série.
         Rempli côté JS par le handler du paramètre URL ?series_parent_id=<id>. -->
    <div id="series-parent-banner" data-series-parent-banner
         style="display:none;background:#eef3fb;border:1px solid #c0d2f5;
                border-radius:0.4rem;padding:0.45rem 0.7rem;margin-bottom:0.8rem;
                font-size:0.85rem;color:#1a2640;">
      Suite de : <strong id="series-parent-title">…</strong>
    </div>
    <input type="hidden" id="series_parent_id" name="series_parent_id" value="" />

    <div class="question">
      <label for="meeting_type">Type de réunion *</label>
      <div class="hint">Le brief sera structuré selon ce type.</div>
      <select id="meeting_type" name="meeting_type" required
              style="width:100%; padding:0.55rem 0.7rem; border:1px solid #d0d5dd;
                     border-radius:6px; font-size:0.92rem; background:#fff;
                     color:#161616; font-family: inherit;">
        <option value="general">Général</option>
        <option value="one_on_one">Entretien 1:1</option>
        <option value="project_update">Point projet / équipe</option>
        <option value="steering_committee">Comité de pilotage (COPIL)</option>
        <option value="brainstorm">Atelier / brainstorm</option>
      </select>
    </div>

    <div class="question">
      <label for="subject">Sujet de la réunion *</label>
      <div class="hint">En une phrase, le thème ou la décision principale.</div>
      <input type="text" id="subject" name="subject" required maxlength="500"
             placeholder="Ex : Arbitrer la trajectoire budgétaire 2027 du programme X">
    </div>

    <div class="question">
      <label for="drive_folder">Dossier Drive</label>
      <div class="hint">Optionnel — si renseigné, les documents seront lus pour enrichir le brief. Collez l'URL du dossier mesfichiers (ou l'identifiant brut).</div>
      <input type="text" id="drive_folder" name="drive_folder"
             placeholder="https://mesfichiers.…/explorer/items/xxxxxxxx">
      <div class="drive-actions" style="display:flex; flex-wrap:wrap; gap:0.5rem; margin-top:0.5rem; align-items:center;">
        {% if drive_base_url %}
        <a id="open-drive-btn" class="btn btn-secondary" style="padding:0.35rem 0.7rem; font-size:0.82rem; text-decoration:none; display:inline-block;"
           href="{{ drive_base_url }}" target="_blank" rel="noopener">
          Ouvrir mes fichiers ↗
        </a>
        {% else %}
        <a id="open-drive-btn" class="btn btn-secondary" style="padding:0.35rem 0.7rem; font-size:0.82rem; text-decoration:none; display:inline-block; opacity:0.5; cursor:not-allowed;"
           href="#" aria-disabled="true" title="DRIVE_BASE_URL non configuré côté serveur"
           onclick="event.preventDefault(); return false;">
          Ouvrir mes fichiers ↗
        </a>
        {% endif %}
        <button type="button" id="test-drive-btn" class="btn btn-secondary"
                style="padding:0.35rem 0.7rem; font-size:0.82rem;">
          Tester l'accès
        </button>
      </div>
      <div id="test-drive-result" aria-live="polite" style="margin-top:0.5rem; font-size:0.82rem;"></div>
    </div>

    <div class="question">
      <label for="role">1. Quel est votre rôle dans cette réunion ? *</label>
      <input type="text" id="role" name="role" required maxlength="300"
             placeholder="J'anime la réunion, je participe, je dois décider…">
      <div class="chips" data-target="role">
        <span class="chip">J'anime la réunion</span>
        <span class="chip">J'y participe</span>
        <span class="chip">Je dois décider</span>
        <span class="chip">Je découvre l'équipe</span>
        <span class="chip">J'observe</span>
      </div>
    </div>

    <div class="question">
      <label for="expectation">2. Qu'attendez-vous principalement de ce brief ? *</label>
      <input type="text" id="expectation" name="expectation" required maxlength="300"
             placeholder="Comprendre le contexte, anticiper les objections…">
      <div class="chips" data-target="expectation">
        <span class="chip">Comprendre le contexte</span>
        <span class="chip">Préparer ma prise de parole</span>
        <span class="chip">Anticiper les objections</span>
        <span class="chip">Valider une décision</span>
        <span class="chip">Apprendre le vocabulaire</span>
      </div>
    </div>

    <div class="question">
      <label>3. Sur quoi concentrer l'analyse ? (plusieurs choix possibles)</label>
      <div class="focus-grid">
        <label><input type="checkbox" name="focus" value="Aspects budgétaires"> Aspects budgétaires</label>
        <label><input type="checkbox" name="focus" value="Risques et points de vigilance"> Risques et points de vigilance</label>
        <label><input type="checkbox" name="focus" value="Décisions à prendre"> Décisions à prendre</label>
        <label><input type="checkbox" name="focus" value="Historique des échanges"> Historique des échanges</label>
        <label><input type="checkbox" name="focus" value="Acronymes et jargon"> Acronymes et jargon</label>
        <label><input type="checkbox" name="focus" value="Parties prenantes"> Parties prenantes</label>
        <label><input type="checkbox" name="focus" value="Calendrier et jalons"> Calendrier et jalons</label>
      </div>
    </div>

    <div class="question">
      <label for="duration">4. Durée prévue de la réunion *</label>
      <input type="text" id="duration" name="duration" required
             placeholder="Ex : 1 heure">
      <div class="chips" data-target="duration">
        <span class="chip" data-minutes="15">15 minutes</span>
        <span class="chip" data-minutes="30">30 minutes</span>
        <span class="chip" data-minutes="60">1 heure</span>
        <span class="chip" data-minutes="120">2 heures</span>
        <span class="chip" data-minutes="240">Demi-journée</span>
        <span class="chip" data-minutes="480">Journée complète</span>
      </div>
    </div>

    <div class="actions">
      <a class="btn btn-secondary" href="/">Annuler</a>
      <button id="submit-btn" class="btn btn-primary" type="submit">Générer le brief</button>
    </div>

    <div id="status" class="status"></div>
  </form>

  <div id="result" class="card" style="display:none;">
    <h1>Brief généré</h1>
    <div id="brief" class="brief-output"></div>
    <h3 style="margin-top:1rem;font-size:0.9rem;color:#1a1a2e;">Documents ingérés</h3>
    <ul id="docs" class="doc-list"></ul>
  </div>
</main>

<script>
  // Chip-to-input behaviour: clicking a chip fills the target text field
  // (and stores a parsed numeric value for the "duration" chips). The user
  // can then edit the filled text freely — chip is suggestion, not lock-in.
  document.querySelectorAll('.chips').forEach(function (group) {
    var targetId = group.getAttribute('data-target');
    var target = document.getElementById(targetId);
    if (!target) return;
    group.querySelectorAll('.chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        target.value = chip.textContent.trim();
        if (chip.dataset.minutes) {
          target.dataset.minutes = chip.dataset.minutes;
        } else {
          delete target.dataset.minutes;
        }
        target.focus();
      });
    });
  });
  // If the user types a custom duration like "45 minutes" or "1h30", we
  // parse it client-side; chip clicks short-circuit by setting dataset.minutes.
  function parseDurationMinutes(raw) {
    if (!raw) return null;
    var s = raw.trim().toLowerCase();
    var explicit = document.getElementById('duration').dataset.minutes;
    if (explicit) {
      var n = parseInt(explicit, 10);
      if (!isNaN(n) && n > 0) return n;
    }
    if (s === 'demi-journée') return 240;
    if (s === 'journée complète' || s === 'journée') return 480;
    var hM = s.match(/^(\d+)\s*h\s*(\d+)?$/);
    if (hM) return parseInt(hM[1], 10) * 60 + (hM[2] ? parseInt(hM[2], 10) : 0);
    var hOnly = s.match(/^(\d+)\s*(heure|heures|h)$/);
    if (hOnly) return parseInt(hOnly[1], 10) * 60;
    var mOnly = s.match(/^(\d+)\s*(minute|minutes|min|m)?$/);
    if (mOnly) return parseInt(mOnly[1], 10);
    return null;
  }

  var statusEl = document.getElementById('status');
  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = 'status ' + (kind || 'info');
  }
  function clearStatus() { statusEl.className = 'status'; statusEl.textContent = ''; }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function renderBrief(brief) {
    var html = '';
    function section(title, body) {
      if (!body) return '';
      return '<div class="brief-section"><h3>' + escapeHtml(title) + '</h3>' + body + '</div>';
    }
    function asUl(items, fmt) {
      if (!Array.isArray(items) || !items.length) return '';
      return '<ul>' + items.map(function (it) {
        return '<li>' + (fmt ? fmt(it) : escapeHtml(it)) + '</li>';
      }).join('') + '</ul>';
    }
    html += section('Objectif reformulé', brief.objective_reformulated
                    ? '<p>' + escapeHtml(brief.objective_reformulated) + '</p>' : '');
    html += section('Contexte', brief.context_recap
                    ? '<p>' + escapeHtml(brief.context_recap) + '</p>' : '');
    html += section('Agenda', asUl(brief.agenda, function (a) {
      return '<strong>' + escapeHtml(a.title || '') + '</strong>'
           + (a.duration_minutes ? ' (' + a.duration_minutes + ' min)' : '')
           + (a.objective ? ' — ' + escapeHtml(a.objective) : '')
           + asUl(a.key_questions);
    }));
    html += section('Fils ouverts', asUl(brief.open_threads, function (t) {
      return escapeHtml(t.item || '') + (t.source ? ' <em>(' + escapeHtml(t.source) + ')</em>' : '');
    }));
    html += section('Notes participants', asUl(brief.participants_notes, function (p) {
      return '<strong>' + escapeHtml(p.name || '') + '</strong> — ' + escapeHtml(p.note || '');
    }));
    html += section("Questions d'ouverture", asUl(brief.opening_questions));
    html += section('Points de vigilance', asUl(brief.risk_points));
    html += section('Checklist de préparation', asUl(brief.preparation_checklist));
    return html || '<p>(Brief vide — le LLM n\'a rien produit d\'exploitable.)</p>';
  }

  function renderDocs(docs) {
    var ul = document.getElementById('docs');
    ul.innerHTML = '';
    (docs || []).forEach(function (d) {
      var li = document.createElement('li');
      li.className = (d.status === 'ingested') ? 'ingested'
                   : (d.status && d.status.indexOf('error') === 0) ? 'error' : 'skipped';
      var label = d.name + ' — ' + d.status;
      if (d.chars) label += ' (' + d.chars + ' car.)';
      li.textContent = label;
      ul.appendChild(li);
    });
  }

  document.getElementById('prep-form').addEventListener('submit', async function (ev) {
    ev.preventDefault();
    clearStatus();
    document.getElementById('result').style.display = 'none';

    var subject = document.getElementById('subject').value.trim();
    var folder = document.getElementById('drive_folder').value.trim();
    var meetingType = document.getElementById('meeting_type').value;
    var role = document.getElementById('role').value.trim();
    var expectation = document.getElementById('expectation').value.trim();
    var duration = parseDurationMinutes(document.getElementById('duration').value);
    var focus = Array.from(document.querySelectorAll('input[name="focus"]:checked'))
                     .map(function (cb) { return cb.value; });

    // Le dossier Drive est désormais optionnel — on ne le bloque plus côté client.
    if (!subject || !role || !expectation) {
      setStatus('Tous les champs marqués * sont requis.', 'err');
      return;
    }
    if (!duration) {
      setStatus('Durée non reconnue — utilisez une suggestion ou un format comme "45 minutes", "1h30".', 'err');
      return;
    }

    var btn = document.getElementById('submit-btn');
    btn.disabled = true;
    setStatus(folder
      ? 'Lecture du Drive et génération du brief en cours…'
      : 'Génération du brief en cours…',
      'info');
    try {
      var spId = (document.getElementById('series_parent_id') || {}).value || '';
      var _bodyObj = {
        subject: subject, drive_folder: folder, role: role,
        expectation: expectation, duration_minutes: duration, focus: focus,
        meeting_type: meetingType,
      };
      if (spId) _bodyObj.series_parent_id = spId;
      var resp = await fetch('/api/preparations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_bodyObj),
      });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) {
        setStatus(data.error || ('Erreur ' + resp.status), 'err');
        return;
      }
      clearStatus();
      document.getElementById('brief').innerHTML = renderBrief(data.brief || {});
      renderDocs(data.documents || []);
      document.getElementById('result').style.display = 'block';
      document.getElementById('result').scrollIntoView({ behavior: 'smooth' });
    } catch (err) {
      setStatus('Erreur réseau : ' + (err && err.message ? err.message : err), 'err');
    } finally {
      btn.disabled = false;
    }
  });

  // Bouton « Tester l'accès » — diagnostic en 3 étapes sur /api/preparations/test-drive.
  var testBtn = document.getElementById('test-drive-btn');
  if (testBtn) {
    testBtn.addEventListener('click', async function () {
      var btn = this;
      var out = document.getElementById('test-drive-result');
      btn.disabled = true;
      out.innerHTML = 'Test en cours…';
      out.style.color = '';
      try {
        // Si l'utilisateur a déjà saisi un dossier Drive, on pousse son
        // id dans la query pour tester aussi le listing children/ — c'est
        // ce probe qui révèle les 403 spécifiques aux sous-collections.
        var folderInput = document.getElementById('drive_folder');
        var folderRaw = folderInput ? folderInput.value.trim() : '';
        var url = '/api/preparations/test-drive';
        if (folderRaw) {
          var match = folderRaw.match(/\/(?:items|folders)\/([^\/?#\s]+)/);
          var folderId = match ? match[1] : folderRaw;
          url += '?folder_id=' + encodeURIComponent(folderId);
        }
        var resp = await fetch(url);
        var data = await resp.json().catch(function () { return {}; });
        var rows = [
          ['Refresh token stocké', !!data.token_stored],
          ['Échange OIDC réussi', !!data.exchange_ok],
          ['Drive accessible', !!data.drive_reachable],
        ];
        if (data.children_probe) {
          var cp = data.children_probe;
          rows.push(['Listing du dossier (' + (cp.status_code || '?') + ')',
                     cp.status_code >= 200 && cp.status_code < 300]);
        }
        out.innerHTML = rows.map(function (r) {
          return '<div>' + (r[1] ? '✅' : '❌') + ' ' + escapeHtml(r[0]) + '</div>';
        }).join('') + (data.error
          ? '<div style="color:#c00;margin-top:0.25rem">' + escapeHtml(data.error) + '</div>'
          : '');
      } catch (e) {
        out.textContent = 'Erreur réseau : ' + (e && e.message ? e.message : e);
        out.style.color = '#c00';
      } finally {
        btn.disabled = false;
      }
    });
  }

  // Meeting-prep v2 §7 — pré-remplissage du wizard depuis ?series_parent_id=<id>.
  // Fetche le brief parent pour afficher "Suite de : <titre>" et pré-remplir
  // sujet/rôle/expectation à partir du parent.
  (async function _prefillFromSeriesParent() {
    try {
      var params = new URLSearchParams(window.location.search || '');
      var pid = params.get('series_parent_id');
      if (!pid) return;
      var hidden = document.getElementById('series_parent_id');
      if (hidden) hidden.value = pid;
      var r = await fetch('/api/preparations/' + encodeURIComponent(pid));
      if (!r.ok) return;
      var d = await r.json();
      var b = (d && (d.preparation || d.brief)) || {};
      var banner = document.getElementById('series-parent-banner');
      var titleEl = document.getElementById('series-parent-title');
      if (banner && titleEl) {
        titleEl.textContent = b.title || b.subject || '(brief parent)';
        banner.style.display = '';
      }
      // Pré-remplissage best-effort.
      var subj = document.getElementById('subject');
      if (subj && !subj.value && b.subject) subj.value = b.subject;
      var roleEl = document.getElementById('role');
      if (roleEl && !roleEl.value && b.role) roleEl.value = b.role;
      var expEl = document.getElementById('expectation');
      if (expEl && !expEl.value && b.expectation) expEl.value = b.expectation;
      var mt = document.getElementById('meeting_type');
      if (mt && b.meeting_type) {
        var found = false;
        for (var i = 0; i < mt.options.length; i++) {
          if (mt.options[i].value === b.meeting_type) { found = true; break; }
        }
        if (found) mt.value = b.meeting_type;
      }
    } catch (e) { /* non-fatal */ }
  })();
</script>
</body>
</html>
"""


# ─── Init & Run ─────────────────────────────────────────────

def create_app():
    global SessionLocal
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    SessionLocal = create_session_factory(db_cfg)
    # PR3 : enregistrement des blueprints extraits.
    _register_modular_blueprints(app)
    return app


def _register_modular_blueprints(flask_app):
    """Attache les blueprints sous ``app/modules/``.

    Garde-fou : ne pas register deux fois (Flask le rejetterait au boot).
    """
    from app.modules.preparations import preparations_bp
    from app.modules.meetings import meetings_bp

    registered = {b.name for b in flask_app.blueprints.values()}
    if "preparations" not in registered:
        flask_app.register_blueprint(preparations_bp)
    if "meetings" not in registered:
        flask_app.register_blueprint(meetings_bp)


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port_value = os.getenv("CODE_GENERATOR_BIND_PORT") or os.getenv("CODE_GENERATOR_PORT", "8080")
    if isinstance(port_value, str) and port_value.startswith("tcp://"):
        port_value = os.getenv("CODE_GENERATOR_BIND_PORT", "8080")
    port = int(port_value)
    application.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "development")
