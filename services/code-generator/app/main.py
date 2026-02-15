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
)
from libs.shared.app.models import ExternalBase, UploadSession, UploadedFile, SessionStatus, UploadStatus
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

oauth = OAuth(app)
oauth.register(
    name="keycloak",
    client_id=oidc_cfg.client_id,
    client_secret=oidc_cfg.client_secret,
    server_metadata_url=f"{oidc_cfg.issuer}/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)
oidc_internal_issuer = os.getenv("OIDC_INTERNAL_ISSUER", oidc_cfg.issuer).rstrip("/")


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
    user: dict, ttl_minutes: int, max_uploads: int, ttl_seconds: int | None = None
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
    return (
        db.query(UploadedFile)
        .join(UploadSession, UploadSession.id == UploadedFile.session_id)
        .filter(UploadedFile.id == file_id, UploadSession.user_sub == user_sub)
        .first()
    )


def _resolve_file_storage(file_obj: UploadedFile):
    if file_obj.transcoded_filename:
        return s3_processed_cfg, file_obj.transcoded_filename
    return s3_upload_cfg, file_obj.stored_filename


def _resolve_source_storage(file_obj: UploadedFile):
    return s3_upload_cfg, file_obj.stored_filename


def _resolve_transcoded_storage(file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None, None
    return s3_processed_cfg, file_obj.transcoded_filename


def _resolve_transferred_storage(db, file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None, None
    session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
    if not session_obj:
        return None, None
    internal_key = f"{session_obj.user_sub}/{session_obj.simple_code}/{file_obj.transcoded_filename}"
    return s3_internal_cfg, internal_key


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

@app.route("/")
@require_auth
def index():
    user = get_current_user()
    return render_template_string(
        INDEX_TEMPLATE,
        user=user,
        short_ttl_enabled=ALLOW_SHORT_QR_TTL_SECONDS_TEST,
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
        "scope": "openid email profile",
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

    # ── Appel au token-issuer INTERNE ──
    try:
        token_data = request_token_from_internal(user, ttl_minutes, max_uploads, ttl_seconds=ttl_seconds)
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
        sessions = db.query(UploadSession).filter(
            UploadSession.user_sub == user["sub"]
        ).order_by(UploadSession.created_at.desc()).limit(20).all()

        reconciled = 0
        result = []
        for s in sessions:
            uploads = []
            for f in s.uploads:
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
                "status": s.status.value,
                "upload_count": s.upload_count,
                "max_uploads": s.max_uploads,
                "expires_at": s.expires_at.isoformat(),
                "created_at": s.created_at.isoformat(),
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
    try:
        devices = request_internal_device_api(
            "GET",
            "/api/v1/devices",
            params={"user_sub": user.get("sub", "")},
        )
        return jsonify(devices if isinstance(devices, list) else [])
    except Exception:
        logger.exception("Failed to list enrolled devices for user %s", user.get("sub"))
        return jsonify({"error": "device_list_unavailable"}), 503


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


@app.route("/api/device/enroll-proxy", methods=["POST"])
def api_device_enroll_proxy():
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        data = request_internal_device_api("POST", "/api/v1/enroll-device", json_body=payload)
        return jsonify(data)
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
        return send_file(
            data,
            mimetype="audio/wav",
            as_attachment=True,
            download_name=f"{Path(file_obj.original_filename).stem}_transcoded.wav",
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
        return send_file(
            data,
            mimetype="audio/wav",
            as_attachment=False,
            download_name=f"{Path(file_obj.original_filename).stem}_transcoded.wav",
        )
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
        return send_file(
            data,
            mimetype="audio/wav",
            as_attachment=True,
            download_name=f"{Path(file_obj.original_filename).stem}_transferred.wav",
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
        return send_file(
            data,
            mimetype="audio/wav",
            as_attachment=False,
            download_name=f"{Path(file_obj.original_filename).stem}_transferred.wav",
        )
    finally:
        db.close()


@app.route("/api/purge-my-sessions", methods=["POST"])
@require_auth
def api_purge_my_sessions():
    user = get_current_user()
    db = SessionLocal()
    deleted_sessions = 0
    deleted_files = 0
    deleted_objects = 0
    try:
        sessions = db.query(UploadSession).filter(UploadSession.user_sub == user["sub"]).all()
        for s in sessions:
            for f in s.uploads:
                try:
                    if f.stored_filename:
                        delete_object(s3_upload_cfg, f.stored_filename)
                        deleted_objects += 1
                except Exception:
                    logger.warning("Failed to delete upload object %s", f.stored_filename)
                try:
                    if f.transcoded_filename:
                        delete_object(s3_processed_cfg, f.transcoded_filename)
                        deleted_objects += 1
                except Exception:
                    logger.warning("Failed to delete processed object %s", f.transcoded_filename)
                deleted_files += 1
            db.delete(s)
            deleted_sessions += 1
        db.commit()
        return jsonify({
            "ok": True,
            "deleted_sessions": deleted_sessions,
            "deleted_files": deleted_files,
            "deleted_objects": deleted_objects,
        })
    except Exception:
        db.rollback()
        logger.exception("Failed to purge user sessions for %s", user["sub"])
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
    <title>Générateur de Code d'Upload Audio</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f0f2f5; color: #1a1a2e; min-height: 100vh;
            display: flex; justify-content: center; align-items: flex-start;
            padding: 2rem 1rem;
        }
        .container { max-width: 520px; width: 100%; }
        .card {
            background: white; border-radius: 16px; padding: 2rem;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08); margin-bottom: 1.5rem;
        }
        h1 { font-size: 1.3rem; margin-bottom: 0.5rem; color: #1a1a2e; }
        .subtitle { color: #666; font-size: 0.9rem; margin-bottom: 1.5rem; }
        .user-info {
            display: flex; align-items: center; justify-content: space-between;
            padding: 0.75rem 1rem; background: #f8f9fa; border-radius: 8px;
            margin-bottom: 1.5rem; font-size: 0.9rem;
        }
        .user-info a { color: #e74c3c; text-decoration: none; font-size: 0.85rem; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.3rem; color: #444; }
        select, input[type=number] {
            width: 100%; padding: 0.6rem; border: 1px solid #ddd; border-radius: 8px;
            font-size: 0.95rem; background: white;
        }
        .btn-primary {
            width: 100%; padding: 0.8rem; border: none; border-radius: 10px;
            background: #2563eb; color: white; font-size: 1rem; font-weight: 600;
            cursor: pointer; transition: background 0.2s;
        }
        .btn-primary:hover { background: #1d4ed8; }
        .btn-primary:disabled { background: #94a3b8; cursor: not-allowed; }
        .btn-danger-mini {
            width: auto; padding: 0.2rem 0.45rem; background: #dc2626;
            font-size: 0.72rem; line-height: 1.1; border-radius: 7px;
        }
        .btn-danger-mini:disabled { background: #cbd5e1; color: #64748b; cursor: not-allowed; }
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
        .session-item {
            padding: 0.75rem; background: #f8f9fa; border-radius: 8px;
            margin-bottom: 0.5rem; font-size: 0.85rem;
        }
        .session-item .code { font-weight: 700; font-family: monospace; color: #2563eb; }
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
            grid-template-columns: repeat(3, 1fr);
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
            display: flex;
            gap: 0.4rem;
            align-items: center;
            color: #334155;
        }
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
            justify-content: space-between;
            align-items: center;
            gap: 0.6rem;
            margin-bottom: 0.5rem;
        }
        .activity-spinner {
            width: 14px;
            height: 14px;
            border: 2px solid #cbd5e1;
            border-top-color: #2563eb;
            border-radius: 999px;
            flex: 0 0 auto;
            opacity: 0.35;
        }
        .activity-spinner.active {
            opacity: 1;
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
            white-space: nowrap;
            border-bottom: 1px dotted #cbd5e1;
        }
        .activity-toggle-link:hover {
            color: #334155;
            border-bottom-color: #94a3b8;
        }
        .recent-activities-panel {
            display: none;
        }
        .recent-activities-panel.open {
            display: block;
        }
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h1>Upload Audio Sécurisé</h1>
        <p class="subtitle">Générez un code pour uploader des fichiers audio depuis votre mobile</p>
        <div class="security-notice">
            Information sécurité: Ne conservez pas durablement des fichiers professionnels
            sur un téléphone personnel. Après la transcription, supprimez les fichiers du téléphone.
        </div>

        <div class="user-info">
            <span>{{ user.name or user.email }}</span>
            <a href="/logout">Déconnexion</a>
        </div>

        <div id="generate-form">
            <div class="form-group">
                <label for="ttl">Durée de validité</label>
                <select id="ttl">
                    {% if short_ttl_enabled %}
                    <option value="15s">15 secondes (test)</option>
                    <option value="30s">30 secondes (test)</option>
                    {% endif %}
                    <option value="15">15 minutes</option>
                    <option value="60">1 heure</option>
                    <option value="240">4 heures</option>
                    <option value="1440">24 heures</option>
                    <option value="4320">3 jours</option>
                </select>
            </div>
            <div class="form-group">
                <label for="max-uploads">Nombre max de fichiers</label>
                <input type="number" id="max-uploads" value="5" min="1" max="50">
            </div>
            <button class="btn-primary" id="btn-generate" onclick="generateCode()">
                Générer un code
            </button>
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
            <button class="btn-primary" style="margin-top:1rem; background:#64748b;"
                    onclick="resetForm()">Générer un nouveau code</button>
        </div>
    </div>

    <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:0.6rem;">
            <h1 style="font-size:1.05rem;">Appareils enrôlés</h1>
            <button class="btn-primary btn-danger-mini" id="revoke-all-devices-btn" onclick="revokeAllDevices()">Révoquer tous</button>
        </div>
        <p class="subtitle" style="margin-top:0.4rem;margin-bottom:0.8rem;">
            Ces appareils peuvent uploader sans rescanner tant que leur enrôlement est valide.
        </p>
        <div id="devices-list" style="font-size:0.84rem;color:#64748b;">Chargement appareils...</div>
    </div>

    <div class="card">
        <div class="activity-inline">
            <span id="activity-spinner" class="activity-spinner" title="Activité en cours"></span>
            <div class="activity-mini">
                <span id="activity-mini-text" class="activity-mini-text">Activités: chargement...</span>
                <div id="activity-rail" class="activity-rail">
                    <span class="activity-dot">1</span><span class="activity-link"></span>
                    <span class="activity-dot">2</span><span class="activity-link"></span>
                    <span class="activity-dot">3</span>
                </div>
            </div>
            <a href="#" id="toggle-activities-link" class="activity-toggle-link"
               onclick="toggleActivitiesPanel(); return false;">Voir activités</a>
        </div>
        <div id="recent-activities-panel" class="recent-activities-panel">
            <div style="display:flex;justify-content:space-between;align-items:center;">
                <h1 style="font-size:1.1rem;">Mes sessions récentes</h1>
                <button id="purge-btn" class="btn-primary btn-danger-mini" disabled
                        onclick="purgeSessions()">Purger liste + fichiers</button>
            </div>
            <div class="transfer-live" id="transfer-live">
                <div class="transfer-live-title">Transferts en cours</div>
                <div class="transfer-live-empty">Chargement...</div>
            </div>
            <div class="sessions-list" id="sessions-list">
                <p style="color:#999; font-size:0.85rem;">Chargement...</p>
            </div>
        </div>
    </div>
</div>

<script>
const impactCache = {};
const impactLoading = new Set();
let activitiesPanelOpen = false;

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

function transferProgressFromMessage(status, msg) {
    if (status === 'transferred') return 100;
    if (status === 'ready_for_transfer') return 10;
    if (status !== 'transferring') return 0;
    const text = (msg || '').toLowerCase();
    const m = text.match(/(\d{1,3})\s*%/);
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

async function loadDevices() {
    const container = document.getElementById('devices-list');
    if (!container) return;
    try {
        const resp = await fetch('/api/my-devices');
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur chargement devices');
        const devices = Array.isArray(data) ? data : [];
        if (!devices.length) {
            container.innerHTML = '<span style="color:#64748b">Aucun appareil enrôle.</span>';
            return;
        }
        container.innerHTML = devices.map((d) => `
            <div style="border:1px solid #e2e8f0;border-radius:8px;padding:0.55rem 0.6rem;margin-bottom:0.5rem;">
                <div style="display:flex;justify-content:space-between;gap:0.5rem;align-items:center;">
                    <div style="min-width:0;">
                        <div style="font-weight:600;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                            ${escapeHtml(d.device_name || 'Appareil sans nom')}
                        </div>
                        <div style="font-size:0.74rem;color:#64748b;">
                            ${escapeHtml(d.status)} | vu: ${escapeHtml(d.last_seen_at || '-')}
                        </div>
                    </div>
                    <button class="btn-primary btn-danger-mini" onclick="revokeDevice('${escapeHtml(d.device_id)}')">Révoquer</button>
                </div>
                <div style="display:flex;gap:0.4rem;margin-top:0.45rem;">
                    <input id="dev-name-${escapeHtml(d.device_id)}" type="text"
                           style="flex:1;padding:0.35rem 0.45rem;border:1px solid #cbd5e1;border-radius:6px;font-size:0.8rem;"
                           placeholder="Renommer l'appareil" value="${escapeHtml(d.device_name || '')}">
                    <button class="btn-primary" style="width:auto;padding:0.35rem 0.5rem;font-size:0.78rem;"
                            onclick="renameDevice('${escapeHtml(d.device_id)}')">Renommer</button>
                </div>
            </div>
        `).join('');
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
    try {
        const resp = await fetch(`/api/my-devices/${deviceId}/revoke`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok || !data.ok) throw new Error(data.error || 'revoke_failed');
        loadDevices();
    } catch (e) {
        alert('Echec révocation appareil.');
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

function toggleActivitiesPanel() {
    activitiesPanelOpen = !activitiesPanelOpen;
    const panel = document.getElementById('recent-activities-panel');
    const link = document.getElementById('toggle-activities-link');
    if (panel) panel.classList.toggle('open', activitiesPanelOpen);
    if (link) link.textContent = activitiesPanelOpen ? 'Masquer activités' : 'Voir activités';
}

async function purgeSessions() {
    const ok = confirm('Supprimer toutes vos sessions et les fichiers associés (S3 upload/processed) ?');
    if (!ok) return;
    try {
        const resp = await fetch('/api/purge-my-sessions', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur purge');
        alert(`Purge terminée: ${data.deleted_sessions} sessions, ${data.deleted_files} fichiers.`);
        loadSessions();
        loadDevices();
    } catch (e) {
        alert('Erreur: ' + e.message);
    }
}

async function loadSessions() {
    try {
        const resp = await fetch('/api/my-sessions');
        const sessions = await resp.json();
        if (!resp.ok) {
            throw new Error((sessions && sessions.error) ? sessions.error : 'Erreur API sessions');
        }
        if (!Array.isArray(sessions)) {
            throw new Error('Format API invalide');
        }
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

        const transfersInProgress = sessions.flatMap(s =>
            ((s.uploads || []).map(f => ({
                sessionCode: s.simple_code,
                name: f.original_filename,
                status: f.status,
                message: f.status_message || '',
                updatedAt: f.updated_at || f.created_at || null,
            })))
        ).filter(f => f.status === 'ready_for_transfer' || f.status === 'transferring');

        if (transferBox) {
            if (transfersInProgress.length === 0) {
                transferBox.innerHTML = `
                    <div class="transfer-live-title">Transferts en cours</div>
                    <div class="transfer-live-empty">Aucun transfert en cours.</div>
                `;
            } else {
                const now = Date.now();
                transferBox.innerHTML = `
                    <div class="transfer-live-title">Transferts en cours (${transfersInProgress.length})</div>
                    <div class="transfer-live-list">
                        ${transfersInProgress.map(t => `
                            <div class="transfer-live-row" title="${escapeHtml(t.name)}">
                                <span class="status-badge file-badge-${escapeHtml(t.status)}">${escapeHtml(statusLabel(t.status))}</span>
                                <span class="transfer-live-code">${escapeHtml(t.sessionCode)}</span>
                                <span class="transfer-live-name">${escapeHtml(t.name)}</span>
                                <span style="color:${(t.updatedAt && (now - new Date(t.updatedAt).getTime()) > 180000) ? '#b91c1c' : '#64748b'};">
                                  ${escapeHtml(t.message || 'Transfert en cours...')}
                                </span>
                            </div>
                        `).join('')}
                    </div>
                `;
            }
        }

        if (sessions.length === 0) {
            container.innerHTML = '<p style="color:#999;font-size:0.85rem;">Aucune session</p>';
            const purgeBtn = document.getElementById('purge-btn');
            if (purgeBtn) purgeBtn.disabled = true;
            return;
        }

        container.innerHTML = sessions.map(s => {
            const isActive = s.status === 'active' && new Date(s.expires_at) > new Date();
            const statusClass = isActive ? 'status-active' : 'status-expired';
            const sessionStatusLabel = isActive ? 'Actif' : 'Expiré';

            const filesHtml = (s.uploads || []).map(f => {
                const quality = (f.audio_quality_score !== null && f.audio_quality_score !== undefined)
                    ? ` <span class="quality-help" title="Indice de qualité audio (1 à 5). Calculé automatiquement par le worker de transcodage selon le niveau RMS, la proportion de silence, la durée et la fréquence d'échantillonnage.">i</span> ${f.audio_quality_score.toFixed(1)}/5`
                    : '';
                const progress = pipelineProgress(f.status, f.status_message);
                const fileStatusClass = `file-badge-${f.status || 'pending'}`;
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
                const sourceLinks = `<div class="file-links file-links-block file-links-row">
                    <div class="file-links-actions">
                        <span class="file-links-title">Source</span>
                        ${f.source_available
                            ? `<a href="${f.source_download_url}" target="_blank" rel="noopener">Télécharger</a>
                               <a href="${f.source_stream_url}" target="_blank" rel="noopener">Écouter</a>`
                            : `<span class="link-disabled" title="Fichier source purgé">Télécharger</span>
                               <span class="link-disabled" title="Fichier source purgé">Écouter</span>`}
                    </div>
                    ${impactIcon}
                </div>`;
                const transcodedLinks = f.transcoded_available
                    ? `<div class="file-links file-links-block">
                        <span class="file-links-title">Transcodé</span>
                        <a href="${f.transcoded_download_url}" target="_blank" rel="noopener">Télécharger</a>
                        <a href="${f.transcoded_stream_url}" target="_blank" rel="noopener">Écouter</a>
                    </div>`
                    : '';
                const transferredLinks = f.transferred_available
                    ? `<div class="file-links file-links-block">
                        <span class="file-links-title">Transféré (interne)</span>
                        <a href="${f.transferred_download_url}" target="_blank" rel="noopener">Télécharger</a>
                        <a href="${f.transferred_stream_url}" target="_blank" rel="noopener">Écouter</a>
                    </div>`
                    : '';
                return `<div class="file-status">
                    <span class="file-name" title="${escapeHtml(f.original_filename)}">${escapeHtml(f.original_filename)}</span>
                    <span class="status-badge ${fileStatusClass}">${escapeHtml(statusLabel(f.status))}</span>${quality}
                    <div class="pipeline-box" title="Progression du pipeline en chemin de fer: analyse, transcodage, transfert">
                        <div class="railroad">
                            <div class="rail-segment ${analyseClass}">
                                <span class="rail-node">1</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment ${transcodeClass}">
                                <span class="rail-node">2</span><span class="rail-line"></span>
                            </div>
                            <div class="rail-segment ${transferClass}">
                                <span class="rail-node">3</span><span class="rail-line rail-line-tail"></span>
                            </div>
                        </div>
                        <div class="rail-labels">
                            <span>Analyse ${progress.scan}%</span>
                            <span>Transcodage ${progress.transcode}%</span>
                            <span>Transfert ${progress.transfer}%</span>
                        </div>
                    </div>
                    ${sourceLinks}
                    ${transcodedLinks}
                    ${transferredLinks}
                </div>`;
            }).join('');

            return `<div class="session-item">
                <span class="code">${s.simple_code}</span>
                <span class="status-badge ${statusClass}">${sessionStatusLabel}</span>
                <span style="float:right;color:#888;">${s.upload_count}/${s.max_uploads} fichiers</span>
                ${filesHtml}
            </div>`;
        }).join('');

        const fileCount = sessions.reduce((acc, s) => acc + ((s.uploads || []).length), 0);
        const purgeBtn = document.getElementById('purge-btn');
        if (purgeBtn) purgeBtn.disabled = fileCount === 0;
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
    }
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
    } catch (e) {
        const msg = `Erreur: ${e.message}`;
        impactCache[fileId] = {
            text: msg,
            at: new Date().toLocaleString('fr-FR'),
        };
    } finally {
        impactLoading.delete(fileId);
        loadSessions();
    }
}

loadSessions();
loadDevices();
setInterval(loadSessions, 15000);
setInterval(loadDevices, 30000);
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
    return app


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port_value = os.getenv("CODE_GENERATOR_BIND_PORT") or os.getenv("CODE_GENERATOR_PORT", "8080")
    if isinstance(port_value, str) and port_value.startswith("tcp://"):
        port_value = os.getenv("CODE_GENERATOR_BIND_PORT", "8080")
    port = int(port_value)
    application.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "development")
