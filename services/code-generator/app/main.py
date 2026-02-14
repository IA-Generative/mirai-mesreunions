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
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import qrcode
import requests as req
from flask import Flask, redirect, url_for, session, render_template_string, jsonify, request, abort, send_file
from authlib.integrations.flask_client import OAuth

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    OIDCConfig, load_ext_db, CODE_TTL_MINUTES, CODE_TTL_MAX_MINUTES,
    MAX_UPLOADS_PER_SESSION, SECRET_KEY, UPLOAD_PORTAL_BASE_URL, load_s3_upload, load_s3_processed,
    UPLOAD_STATUS_VIEW_TTL_MINUTES, TOKEN_ISSUER_API_URL, INTERNAL_API_TOKEN,
)
from libs.shared.app.models import ExternalBase, UploadSession, UploadedFile, SessionStatus, UploadStatus
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret
from libs.shared.app.s3_helper import download_fileobj, delete_object

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# ─── Flask App ──────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY

oidc_cfg = OIDCConfig()
db_cfg = load_ext_db()
s3_upload_cfg = load_s3_upload()
s3_processed_cfg = load_s3_processed()
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
    redirect_uri = oidc_cfg.redirect_uri
    return oauth.keycloak.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.keycloak.authorize_access_token()
    userinfo = token.get("userinfo", {})
    session["user"] = {
        "sub": userinfo.get("sub", ""),
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", userinfo.get("preferred_username", "")),
    }
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


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

        result = []
        for s in sessions:
            uploads = []
            for f in s.uploads:
                uploads.append({
                    "id": str(f.id),
                    "original_filename": f.original_filename,
                    "status": f.status.value,
                    "status_message": f.status_message,
                    "audio_quality_score": f.audio_quality_score,
                    "created_at": f.created_at.isoformat(),
                    "download_url": f"/api/file/download/{f.id}",
                    "stream_url": f"/api/file/stream/{f.id}",
                    "source_download_url": f"/api/file/download-source/{f.id}",
                    "source_stream_url": f"/api/file/stream-source/{f.id}",
                    "transcoded_available": bool(f.transcoded_filename),
                    "transcoded_download_url": f"/api/file/download-transcoded/{f.id}" if f.transcoded_filename else None,
                    "transcoded_stream_url": f"/api/file/stream-transcoded/{f.id}" if f.transcoded_filename else None,
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
        return jsonify(result)
    finally:
        db.close()


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
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h1>Upload Audio Sécurisé</h1>
        <p class="subtitle">Générez un code pour uploader des fichiers audio depuis votre mobile</p>

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
        <div style="display:flex;justify-content:space-between;align-items:center;">
            <h1 style="font-size:1.1rem;">Mes sessions récentes</h1>
            <button id="purge-btn" class="btn-primary btn-danger-mini" disabled
                    onclick="purgeSessions()">Purger liste + fichiers</button>
        </div>
        <div class="sessions-list" id="sessions-list">
            <p style="color:#999; font-size:0.85rem;">Chargement...</p>
        </div>
    </div>
</div>

<script>
const impactCache = {};
const impactLoading = new Set();

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

function pipelineProgress(status) {
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
            p.transfer = 10;
            p.active = 'transfert';
            break;
        case 'transferring':
            p.scan = 100;
            p.transcode = 100;
            p.transfer = 50;
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

async function purgeSessions() {
    const ok = confirm('Supprimer toutes vos sessions et les fichiers associés (S3 upload/processed) ?');
    if (!ok) return;
    try {
        const resp = await fetch('/api/purge-my-sessions', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Erreur purge');
        alert(`Purge terminée: ${data.deleted_sessions} sessions, ${data.deleted_files} fichiers.`);
        loadSessions();
    } catch (e) {
        alert('Erreur: ' + e.message);
    }
}

async function loadSessions() {
    try {
        const resp = await fetch('/api/my-sessions');
        const sessions = await resp.json();
        const container = document.getElementById('sessions-list');

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

            const filesHtml = s.uploads.map(f => {
                const quality = (f.audio_quality_score !== null && f.audio_quality_score !== undefined)
                    ? ` <span class="quality-help" title="Indice de qualité audio (1 à 5). Calculé automatiquement par le worker de transcodage selon le niveau RMS, la proportion de silence, la durée et la fréquence d'échantillonnage.">i</span> ${f.audio_quality_score.toFixed(1)}/5`
                    : '';
                const progress = pipelineProgress(f.status);
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
                        <a href="${f.source_download_url}" target="_blank" rel="noopener">Télécharger</a>
                        <a href="${f.source_stream_url}" target="_blank" rel="noopener">Écouter</a>
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
        if (container) {
            container.innerHTML = '<p style="color:#b91c1c;font-size:0.85rem;">Erreur chargement sessions. Rechargez la page.</p>';
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
setInterval(loadSessions, 15000);
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
    port = int(os.getenv("CODE_GENERATOR_PORT", 8080))
    application.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "development")
