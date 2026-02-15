"""
Upload Portal Service
=====================
Public-facing page accessible via QR code or simple code.
Handles audio file uploads with real-time status feedback.
No authentication required - access is controlled by the code/token.
"""

import logging
import os
import sys
import threading
import time
import json
import requests as req
from datetime import datetime, timezone, timedelta
from io import BytesIO
from uuid import uuid4

from flask import Flask, request, jsonify, render_template, abort, redirect, url_for, send_from_directory
from flask_socketio import SocketIO, emit, join_room
from sqlalchemy import text
from werkzeug.utils import secure_filename

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_ext_db, load_s3_upload, load_s3_processed, RabbitMQConfig, SECRET_KEY,
    MAX_UPLOADS_PER_SESSION, UPLOAD_MAX_FILE_SIZE_MB, ALLOWED_AUDIO_EXTENSIONS,
    UPLOAD_STATUS_VIEW_TTL_MINUTES, INTERNAL_API_TOKEN, UPLOAD_EXPIRY_GRACE_SECONDS,
)
from libs.shared.app.models import ExternalBase, UploadSession, UploadedFile, SessionStatus, UploadStatus
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.s3_helper import upload_fileobj, ensure_bucket, delete_object
from libs.shared.app.queue_helper import publish_message, QUEUE_AV_SCAN, RabbitMQConfig
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.device_token import verify_device_token, utc_now_ts

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

db_cfg = load_ext_db()
s3_cfg = load_s3_upload()
s3_processed_cfg = load_s3_processed()
rabbit_cfg = RabbitMQConfig()
SessionLocal = None
_purge_thread_started = False

EXTERNAL_PURGE_INTERVAL_SECONDS = max(60, int(os.getenv("EXTERNAL_PURGE_INTERVAL_SECONDS", "86400")))
EXTERNAL_PURGE_MAX_AGE_HOURS = max(1, int(os.getenv("EXTERNAL_PURGE_MAX_AGE_HOURS", "12")))
EXTERNAL_PURGE_LOCK_ID = int(os.getenv("EXTERNAL_PURGE_LOCK_ID", "810018001"))
DEVICE_API_PROXY_BASE_URL = os.getenv("DEVICE_API_PROXY_BASE_URL", "http://code-generator:8080").rstrip("/")
TOKEN_ISSUER_ENROLL_DEVICE_URL = f"{DEVICE_API_PROXY_BASE_URL}/api/device/enroll-proxy"
TOKEN_ISSUER_VALIDATE_DEVICE_URL = f"{DEVICE_API_PROXY_BASE_URL}/api/device/validate-proxy"
DEVICE_REVALIDATE_INTERVAL_SECONDS = max(60, int(os.getenv("DEVICE_REVALIDATE_INTERVAL_SECONDS", "14400")))
DEVICE_REVALIDATE_MAX_FAILURE_SECONDS = max(300, int(os.getenv("DEVICE_REVALIDATE_MAX_FAILURE_SECONDS", "14400")))
_device_validation_state = {}
_device_validation_lock = threading.Lock()


# ─── Helpers ────────────────────────────────────────────────

def get_session_by_token(qr_token: str):
    db = SessionLocal()
    try:
        return db.query(UploadSession).filter(UploadSession.qr_token == qr_token).first()
    finally:
        db.close()


def get_session_by_code(simple_code: str):
    db = SessionLocal()
    try:
        code = simple_code.upper().strip()
        return db.query(UploadSession).filter(UploadSession.simple_code == code).first()
    finally:
        db.close()


def is_session_valid(session_obj, for_upload: bool = False) -> tuple:
    """Check if session is valid. Returns (is_valid, reason)."""
    if not session_obj:
        return False, "Code invalide ou introuvable."

    now = datetime.now(timezone.utc)
    renewal_hint = "Veuillez renouveler votre token (durée + téléchargements) dans l'interface admin."

    expires_at = session_obj.expires_at.replace(tzinfo=timezone.utc)
    if for_upload and UPLOAD_EXPIRY_GRACE_SECONDS > 0:
        expires_at = expires_at + timedelta(seconds=UPLOAD_EXPIRY_GRACE_SECONDS)

    if expires_at < now:
        if for_upload and UPLOAD_EXPIRY_GRACE_SECONDS > 0:
            return False, (
                f"Token expiré (fenêtre de grâce de {UPLOAD_EXPIRY_GRACE_SECONDS}s dépassée). "
                f"{renewal_hint}"
            )
        return False, f"Token expiré. {renewal_hint}"

    if session_obj.status != SessionStatus.ACTIVE:
        return False, f"Token révoqué ou inactif. {renewal_hint}"

    if session_obj.upload_count >= session_obj.max_uploads:
        return False, (
            f"Nombre maximal de téléchargements atteint ({session_obj.max_uploads}/{session_obj.max_uploads}). "
            f"{renewal_hint}"
        )

    return True, "OK"


def can_view_status(session_obj) -> bool:
    """Check if status can still be viewed (even after upload limit reached)."""
    if not session_obj:
        return False
    now = datetime.now(timezone.utc)
    if session_obj.status_view_expires_at:
        return session_obj.status_view_expires_at.replace(tzinfo=timezone.utc) > now
    return True


def allowed_file(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_AUDIO_EXTENSIONS


def _extract_device_token() -> str:
    return (request.headers.get("X-Device-Token") or "").strip()


def _set_validation_state(device_id: str, ok: bool):
    now_ts = utc_now_ts()
    with _device_validation_lock:
        state = _device_validation_state.get(device_id, {})
        if ok:
            _device_validation_state[device_id] = {
                "last_ok_ts": now_ts,
                "last_check_ts": now_ts,
                "last_error": "",
                "first_failure_ts": None,
                "backend_invalid": False,
                "backend_reason": "",
            }
            return
        first_failure_ts = state.get("first_failure_ts") or now_ts
        _device_validation_state[device_id] = {
            "last_ok_ts": state.get("last_ok_ts"),
            "last_check_ts": now_ts,
            "last_error": "validate_failed",
            "first_failure_ts": first_failure_ts,
            "backend_invalid": False,
            "backend_reason": "",
        }


def _set_validation_state_invalid(device_id: str, reason: str):
    now_ts = utc_now_ts()
    with _device_validation_lock:
        state = _device_validation_state.get(device_id, {})
        _device_validation_state[device_id] = {
            "last_ok_ts": state.get("last_ok_ts"),
            "last_check_ts": now_ts,
            "last_error": "backend_invalid",
            "first_failure_ts": None,
            "backend_invalid": True,
            "backend_reason": (reason or "invalid").strip(),
        }


def _get_validation_state(device_id: str) -> dict:
    with _device_validation_lock:
        return dict(_device_validation_state.get(device_id, {}))


def _async_validate_device(device_token: str, qr_token: str):
    payload = {"device_token": device_token, "qr_token": qr_token}
    try:
        resp = req.post(
            TOKEN_ISSUER_VALIDATE_DEVICE_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=6,
        )
        if resp.status_code >= 400:
            reason = ""
            try:
                body = resp.json()
                reason = str(body.get("reason") or body.get("error") or "").strip()
            except Exception:
                reason = ""
            try:
                device_id = verify_device_token(device_token, INTERNAL_API_TOKEN).get("device_id", "")
            except Exception:
                return
            # 4xx with explicit device-invalid reasons should block immediately.
            definitive_reasons = {
                "revoked",
                "not_found",
                "token_mismatch",
                "qr_expired",
                "retention_expired",
                "invalid_signature",
                "invalid_payload",
            }
            if resp.status_code < 500 and reason in definitive_reasons:
                _set_validation_state_invalid(device_id, reason)
            else:
                _set_validation_state(device_id, ok=False)
            return
        data = resp.json()
        device_id = str(data.get("device_id") or "")
        if device_id:
            if bool(data.get("valid")):
                _set_validation_state(device_id, ok=True)
            else:
                _set_validation_state_invalid(device_id, str(data.get("reason") or "invalid"))
    except Exception:
        try:
            device_id = verify_device_token(device_token, INTERNAL_API_TOKEN).get("device_id", "")
        except Exception:
            return
        if device_id:
            _set_validation_state(device_id, ok=False)


def _sync_validate_device_once(device_token: str, qr_token: str) -> tuple[bool, bool, str]:
    """Best-effort strong backend validation on session bootstrap."""
    try:
        resp = req.post(
            TOKEN_ISSUER_VALIDATE_DEVICE_URL,
            json={"device_token": device_token, "qr_token": qr_token},
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=6,
        )
    except Exception:
        try:
            device_id = str(verify_device_token(device_token, INTERNAL_API_TOKEN).get("device_id") or "")
        except Exception:
            device_id = ""
        if device_id:
            _set_validation_state(device_id, ok=False)
        return False, True, "backend_unavailable"

    reason = ""
    data = {}
    try:
        data = resp.json()
        reason = str(data.get("reason") or data.get("error") or "").strip()
    except Exception:
        reason = ""

    try:
        device_id = str(verify_device_token(device_token, INTERNAL_API_TOKEN).get("device_id") or "")
    except Exception:
        device_id = ""

    if resp.status_code >= 500:
        if device_id:
            _set_validation_state(device_id, ok=False)
        return False, True, "backend_unavailable"

    if bool(data.get("valid")):
        if device_id:
            _set_validation_state(device_id, ok=True)
        return True, True, "ok"

    if device_id:
        _set_validation_state_invalid(device_id, reason or "invalid")
    return True, False, reason or "invalid"


def _validate_device_fast_path(qr_token: str, device_token: str) -> tuple[bool, dict]:
    if not device_token:
        return False, {"reason": "missing_device_token", "message": "Appareil non enrole."}
    try:
        payload = verify_device_token(device_token, INTERNAL_API_TOKEN)
    except Exception:
        return False, {"reason": "invalid_device_token", "message": "Token appareil invalide."}

    if str(payload.get("qr_token") or "").strip() != qr_token:
        return False, {"reason": "token_scope_mismatch", "message": "Token appareil non associe a ce code."}

    now_ts = utc_now_ts()
    retention_until = int(payload.get("retention_until") or 0)
    if retention_until and now_ts > retention_until:
        return False, {"reason": "retention_expired", "message": "Enrolement expire. Scannez un nouveau code."}

    device_id = str(payload.get("device_id") or "")
    if not device_id:
        return False, {"reason": "missing_device_id", "message": "Token appareil incomplet."}

    state = _get_validation_state(device_id)
    if bool(state.get("backend_invalid")):
        reason = str(state.get("backend_reason") or "invalid")
        renewal_hint = "Veuillez renouveler votre token (durée + téléchargements) dans l'interface admin."
        message_by_reason = {
            "revoked": f"Token révoqué. {renewal_hint}",
            "qr_expired": f"Token expiré. {renewal_hint}",
            "retention_expired": f"Token expiré. {renewal_hint}",
            "not_found": f"Appareil inconnu pour ce token. {renewal_hint}",
            "token_mismatch": f"Token invalide pour cette session. {renewal_hint}",
        }
        return False, {
            "reason": reason,
            "message": message_by_reason.get(
                reason,
                f"Token invalide. {renewal_hint}",
            ),
        }

    last_check_ts = int(state.get("last_check_ts") or 0)
    first_failure_ts = state.get("first_failure_ts")
    if first_failure_ts:
        elapsed = now_ts - int(first_failure_ts)
        if elapsed > DEVICE_REVALIDATE_MAX_FAILURE_SECONDS:
            return False, {
                "reason": "backend_validation_window_exceeded",
                "message": (
                    "Validation backend indisponible trop longtemps. "
                    "Pour votre securite, retournez sur le backend et regenerez un code."
                ),
            }

    retry_interval = DEVICE_REVALIDATE_INTERVAL_SECONDS
    if first_failure_ts:
        retry_interval = min(DEVICE_REVALIDATE_INTERVAL_SECONDS, 300)
    if now_ts - last_check_ts >= retry_interval:
        t = threading.Thread(target=_async_validate_device, args=(device_token, qr_token), daemon=True)
        t.start()

    return True, {"reason": "ok", "device_id": device_id}


def run_external_purge_once():
    """Purge old uploaded files from external DB and S3."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=EXTERNAL_PURGE_MAX_AGE_HOURS)
    db = SessionLocal()
    removed_db = 0
    removed_s3 = 0
    skipped_s3 = 0
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": EXTERNAL_PURGE_LOCK_ID},
            ).scalar()
        )
        if not lock_acquired:
            logger.debug("External purge skipped (lock busy)")
            return

        stale_files = (
            db.query(UploadedFile)
            .filter(UploadedFile.created_at < cutoff)
            .all()
        )

        for file_obj in stale_files:
            if file_obj.transcoded_filename:
                try:
                    delete_object(s3_processed_cfg, file_obj.transcoded_filename)
                    removed_s3 += 1
                except Exception:
                    skipped_s3 += 1
                    logger.warning("Failed to delete processed object: %s", file_obj.transcoded_filename)

            try:
                delete_object(s3_cfg, file_obj.stored_filename)
                removed_s3 += 1
            except Exception:
                skipped_s3 += 1
                logger.warning("Failed to delete upload object: %s", file_obj.stored_filename)

            db.delete(file_obj)
            removed_db += 1

        db.commit()
        if removed_db:
            logger.info(
                "External purge done: db=%d, s3_deleted=%d, s3_failed=%d, cutoff=%s",
                removed_db, removed_s3, skipped_s3, cutoff.isoformat()
            )
    except Exception:
        db.rollback()
        logger.exception("External purge failed")
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": EXTERNAL_PURGE_LOCK_ID},
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.warning("Failed to release external purge lock")
        db.close()


def _purge_loop():
    logger.info(
        "Starting external purge loop: interval=%ss, max_age=%sh",
        EXTERNAL_PURGE_INTERVAL_SECONDS,
        EXTERNAL_PURGE_MAX_AGE_HOURS,
    )
    while True:
        run_external_purge_once()
        time.sleep(EXTERNAL_PURGE_INTERVAL_SECONDS)


# ─── Routes ─────────────────────────────────────────────────

@app.route("/")
def index():
    """Landing page with code input form."""
    return render_template("upload_landing.html")


@app.route("/manifest/<qr_token>.webmanifest")
def upload_manifest(qr_token: str):
    """Dynamic manifest bound to a specific QR token upload page."""
    session_obj = get_session_by_token(qr_token)
    if not session_obj:
        abort(404)
    start_url = url_for("upload_page", qr_token=qr_token)
    data = {
        "name": "MIrAI - Televersement audio",
        "short_name": "MIrAI Audio",
        "description": "Televersement audio facilite et securise.",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "background_color": "#f6f6f6",
        "theme_color": "#000091",
        "icons": [
            {
                "src": url_for("static", filename="icons/pwa-icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": url_for("static", filename="icons/pwa-icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return app.response_class(
        json.dumps(data, ensure_ascii=True),
        mimetype="application/manifest+json",
    )


@app.route("/sw.js")
def service_worker():
    """Serve service worker at root scope for PWA install."""
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "static"),
        "sw.js",
        mimetype="application/javascript",
    )


@app.route("/code", methods=["POST"])
def code_lookup():
    """Redirect to upload page from simple code."""
    code = request.form.get("code", "").strip().upper()
    session_obj = get_session_by_code(code)
    if not session_obj:
        return render_template("upload_landing.html", error="Code invalide.")
    return redirect(f"/upload/{session_obj.qr_token}")


@app.route("/upload/<qr_token>")
def upload_page(qr_token):
    """Main upload page, accessed via QR code URL."""
    session_obj = get_session_by_token(qr_token)
    if not session_obj:
        return render_template("upload_error.html", message="Lien invalide ou introuvable."), 404

    valid, reason = is_session_valid(session_obj)
    can_view = can_view_status(session_obj)

    db = SessionLocal()
    try:
        uploads = db.query(UploadedFile).filter(
            UploadedFile.session_id == session_obj.id
        ).order_by(UploadedFile.created_at.desc()).all()

        upload_list = [{
            "id": str(f.id),
            "name": f.original_filename,
            "status": f.status.value,
            "message": f.status_message or "",
            "quality": f.audio_quality_score,
        } for f in uploads]
    finally:
        db.close()

    return render_template(
        "upload_page.html",
        qr_token=qr_token,
        simple_code=session_obj.simple_code,
        can_upload=valid,
        can_view=can_view,
        reason=reason if not valid else "",
        remaining=max(0, session_obj.max_uploads - session_obj.upload_count),
        max_uploads=session_obj.max_uploads,
        uploads=upload_list,
        allowed_extensions=",".join(f".{e}" for e in ALLOWED_AUDIO_EXTENSIONS),
        device_revalidate_interval_seconds=DEVICE_REVALIDATE_INTERVAL_SECONDS,
        device_revalidate_max_failure_seconds=DEVICE_REVALIDATE_MAX_FAILURE_SECONDS,
    )


@app.route("/api/device/session/<qr_token>")
def api_device_session(qr_token):
    """Session bootstrap for device enrollment flow."""
    session_obj = get_session_by_token(qr_token)
    if not session_obj:
        return jsonify({"error": "Lien invalide ou introuvable."}), 404

    valid, reason = is_session_valid(session_obj)
    can_view = can_view_status(session_obj)
    device_token = _extract_device_token()
    ok, details = _validate_device_fast_path(qr_token, device_token)
    if ok and device_token:
        backend_available, backend_valid, backend_reason = _sync_validate_device_once(device_token, qr_token)
        if backend_available and not backend_valid:
            ok = False
            details = {
                "reason": backend_reason,
                "message": (
                    "Token invalide, expiré ou révoqué. "
                    "Veuillez renouveler votre token (durée + téléchargements) dans l'interface admin."
                ),
            }
    status = "enrolled" if ok else "needs_enrollment"
    if not valid and not can_view:
        status = "session_unavailable"
    if device_token and not ok and details.get("reason") != "missing_device_token":
        return jsonify(
            {
                "status": status,
                "can_upload": False,
                "can_view": can_view,
                "session_reason": "" if valid else reason,
                "device_reason": details.get("reason"),
                "device_message": details.get("message"),
                "simple_code": session_obj.simple_code,
                "expires_at": session_obj.expires_at.isoformat() if session_obj.expires_at else None,
                "revalidate_interval_seconds": DEVICE_REVALIDATE_INTERVAL_SECONDS,
                "max_validation_failure_seconds": DEVICE_REVALIDATE_MAX_FAILURE_SECONDS,
            }
        ), 401

    return jsonify(
        {
            "status": status,
            "can_upload": valid and (ok or not device_token),
            "can_view": can_view,
            "session_reason": "" if valid else reason,
            "device_reason": details.get("reason"),
            "device_message": details.get("message"),
            "simple_code": session_obj.simple_code,
            "expires_at": session_obj.expires_at.isoformat() if session_obj.expires_at else None,
            "revalidate_interval_seconds": DEVICE_REVALIDATE_INTERVAL_SECONDS,
            "max_validation_failure_seconds": DEVICE_REVALIDATE_MAX_FAILURE_SECONDS,
        }
    )


@app.route("/api/device/enroll/<qr_token>", methods=["POST"])
def api_device_enroll(qr_token):
    """Enroll this browser/device for a QR session."""
    session_obj = get_session_by_token(qr_token)
    valid, reason = is_session_valid(session_obj)
    if not valid:
        return jsonify({"error": reason}), 400

    data = request.get_json(silent=True) or {}
    payload = {
        "qr_token": qr_token,
        "device_key": (data.get("device_key") or "").strip(),
        "device_fingerprint": (data.get("device_fingerprint") or "").strip(),
        "device_name": (data.get("device_name") or "").strip() or None,
    }
    if not payload["device_key"]:
        return jsonify({"error": "device_key manquant"}), 400

    try:
        resp = req.post(
            TOKEN_ISSUER_ENROLL_DEVICE_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=8,
        )
        body = resp.json() if resp.headers.get("Content-Type", "").startswith("application/json") else {}
        if resp.status_code >= 400:
            return jsonify({"error": body.get("error", "enrollment_failed")}), resp.status_code
        return jsonify(body)
    except Exception:
        logger.exception("Device enrollment failed for qr=%s", qr_token)
        return jsonify({"error": "enrollment_unavailable"}), 503


@app.route("/api/upload/<qr_token>", methods=["POST"])
def api_upload(qr_token):
    """Handle file upload via API."""
    session_obj = get_session_by_token(qr_token)
    valid, reason = is_session_valid(session_obj, for_upload=True)

    if not valid:
        return jsonify({"error": reason}), 400
    ok, details = _validate_device_fast_path(qr_token, _extract_device_token())
    if not ok:
        return jsonify(
            {
                "error": details.get("message", "Device non enrole ou invalide."),
                "device_reason": details.get("reason"),
            }
        ), 401

    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier sélectionné."}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Nom de fichier vide."}), 400

    if not allowed_file(file.filename):
        return jsonify({
            "error": f"Format non supporté. Formats acceptés : {', '.join(ALLOWED_AUDIO_EXTENSIONS)}"
        }), 400

    # Read file
    file_data = file.read()
    file_size = len(file_data)

    if file_size == 0:
        return jsonify({"error": "Fichier vide."}), 400

    # Build stored filename: {simple_code}_{uuid}_{original_name}
    safe_name = secure_filename(file.filename)
    stored_name = f"{session_obj.simple_code}_{uuid4().hex[:8]}_{safe_name}"

    # Upload to S3
    try:
        upload_fileobj(
            s3_cfg,
            stored_name,
            BytesIO(file_data),
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        logger.exception("S3 upload failed")
        return jsonify({"error": "Erreur lors de l'upload. Réessayez."}), 500

    # Create DB record
    db = SessionLocal()
    try:
        uploaded_file = UploadedFile(
            id=uuid4(),
            session_id=session_obj.id,
            original_filename=file.filename,
            stored_filename=stored_name,
            file_size_bytes=file_size,
            mime_type=file.content_type,
            status=UploadStatus.PENDING,
            status_message="Fichier reçu, en attente d'analyse antivirale...",
        )
        db.add(uploaded_file)

        # Update session upload count
        session_obj = db.query(UploadSession).filter(UploadSession.id == session_obj.id).first()
        session_obj.upload_count += 1
        db.commit()

        file_id = str(uploaded_file.id)
    finally:
        db.close()

    # Publish to antivirus queue
    try:
        publish_message(rabbit_cfg, QUEUE_AV_SCAN, {
            "file_id": file_id,
            "session_id": str(session_obj.id),
            "stored_filename": stored_name,
            "original_filename": file.filename,
            "simple_code": session_obj.simple_code,
            "user_sub": session_obj.user_sub,
            "user_email": session_obj.user_email,
        })
    except Exception as e:
        logger.exception("Failed to publish to queue")

    # Notify via WebSocket
    socketio.emit("file_status", {
        "file_id": file_id,
        "name": file.filename,
        "status": "pending",
        "message": "Fichier reçu, en attente d'analyse antivirale...",
    }, room=qr_token)

    return jsonify({
        "file_id": file_id,
        "filename": file.filename,
        "status": "pending",
        "remaining": max(0, session_obj.max_uploads - session_obj.upload_count),
    })


@app.route("/api/status/<qr_token>")
def api_status(qr_token):
    """Get current status of all files in session."""
    session_obj = get_session_by_token(qr_token)
    if not session_obj or not can_view_status(session_obj):
        return jsonify({"error": "Session introuvable ou expirée."}), 404
    ok, details = _validate_device_fast_path(qr_token, _extract_device_token())
    if not ok:
        return jsonify(
            {
                "error": details.get("message", "Device non enrole ou invalide."),
                "device_reason": details.get("reason"),
            }
        ), 401

    db = SessionLocal()
    try:
        uploads = db.query(UploadedFile).filter(
            UploadedFile.session_id == session_obj.id
        ).order_by(UploadedFile.created_at.desc()).all()

        return jsonify({
            "files": [{
                "id": str(f.id),
                "name": f.original_filename,
                "status": f.status.value,
                "message": f.status_message or "",
                "quality": f.audio_quality_score,
            } for f in uploads],
            "can_upload": is_session_valid(session_obj, for_upload=True)[0],
            "remaining": max(0, session_obj.max_uploads - session_obj.upload_count),
        })
    finally:
        db.close()


# ─── WebSocket ──────────────────────────────────────────────

@socketio.on("join")
def on_join(data):
    """Join a room based on qr_token for real-time updates."""
    room = data.get("qr_token")
    if room:
        join_room(room)
        emit("joined", {"room": room})


# ─── Notification endpoint (called by workers) ─────────────

@app.route("/api/notify-status", methods=["POST"])
def notify_status():
    """Called by workers to persist status updates and push WebSocket events."""
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        logger.warning("Unauthorized notify-status request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400
    file_id = data.get("file_id")
    status_raw = data.get("status")
    status_msg = data.get("message", "")
    quality = data.get("quality")
    qr_token = data.get("qr_token")

    db = SessionLocal()
    try:
        if file_id:
            file_obj = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
            if file_obj:
                if status_raw:
                    try:
                        file_obj.status = UploadStatus(status_raw)
                    except Exception:
                        logger.warning("Invalid status in notify-status: %s", status_raw)
                file_obj.status_message = status_msg
                if quality is not None:
                    file_obj.audio_quality_score = quality
                db.commit()

                if not qr_token:
                    sess = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
                    if sess:
                        qr_token = sess.qr_token
            else:
                logger.warning("notify-status file not found: %s", file_id)
    except Exception:
        db.rollback()
        logger.exception("notify-status DB update failed for file_id=%s", file_id)
    finally:
        db.close()

    if qr_token:
        socketio.emit("file_status", {
            "file_id": file_id,
            "name": data.get("filename"),
            "status": status_raw,
            "message": status_msg,
            "quality": quality,
        }, room=qr_token)
    return jsonify({"ok": True})


# ─── Init ───────────────────────────────────────────────────

def create_app():
    global SessionLocal, _purge_thread_started
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    SessionLocal = create_session_factory(db_cfg)
    ensure_bucket(s3_cfg)
    ensure_bucket(s3_processed_cfg)
    if not _purge_thread_started:
        purge_thread = threading.Thread(target=_purge_loop, daemon=True, name="external-purge-loop")
        purge_thread.start()
        _purge_thread_started = True
    return app


# WSGI/ASGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port = int(os.getenv("UPLOAD_PORTAL_PORT", 8081))
    socketio.run(
        application,
        host="0.0.0.0",
        port=port,
        debug=os.getenv("ENVIRONMENT") == "development",
        allow_unsafe_werkzeug=True,
    )
