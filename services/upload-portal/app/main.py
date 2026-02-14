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
from datetime import datetime, timezone, timedelta
from io import BytesIO
from uuid import uuid4

from flask import Flask, request, jsonify, render_template, abort, redirect, url_for
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

    expires_at = session_obj.expires_at.replace(tzinfo=timezone.utc)
    if for_upload and UPLOAD_EXPIRY_GRACE_SECONDS > 0:
        expires_at = expires_at + timedelta(seconds=UPLOAD_EXPIRY_GRACE_SECONDS)

    if expires_at < now:
        if for_upload and UPLOAD_EXPIRY_GRACE_SECONDS > 0:
            return False, (
                f"Ce code a expiré (fenêtre de grâce de {UPLOAD_EXPIRY_GRACE_SECONDS}s dépassée)."
            )
        return False, "Ce code a expiré."

    if session_obj.status != SessionStatus.ACTIVE:
        return False, "Cette session n'est plus active."

    if session_obj.upload_count >= session_obj.max_uploads:
        return False, f"Nombre maximum de fichiers atteint ({session_obj.max_uploads})."

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
    )


@app.route("/api/upload/<qr_token>", methods=["POST"])
def api_upload(qr_token):
    """Handle file upload via API."""
    session_obj = get_session_by_token(qr_token)
    valid, reason = is_session_valid(session_obj, for_upload=True)

    if not valid:
        return jsonify({"error": reason}), 400

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
    """Called by workers to push status updates via WebSocket."""
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        logger.warning("Unauthorized notify-status request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400
    qr_token = data.get("qr_token")
    if qr_token:
        socketio.emit("file_status", {
            "file_id": data.get("file_id"),
            "name": data.get("filename"),
            "status": data.get("status"),
            "message": data.get("message", ""),
            "quality": data.get("quality"),
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
