"""
File Puller Service (Zone Interne)
==================================
API endpoint that receives notifications from the external zone.
Upon notification, it PULLS the transcoded file from processed-staging S3
and stores it in internal-storage S3.

SECURITY: Only entry point into the internal zone.
- Only accepts authenticated API calls with bearer token
- INITIATES file transfer (pull), never accepts pushed data
- Notification only contains metadata, not file content
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from io import BytesIO
from uuid import uuid4

import requests as req
from flask import Flask, request, jsonify
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_int_db, load_s3_processed, load_s3_internal,
    INTERNAL_API_TOKEN, RabbitMQConfig,
)
from libs.shared.app.models import InternalBase, UserAudioFile
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.s3_helper import download_fileobj, upload_fileobj, ensure_bucket, delete_object
from libs.shared.app.queue_helper import publish_message, declare_queues, QUEUE_TRANSCRIPTION
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

db_cfg = load_int_db()
s3_processed_cfg = load_s3_processed()
s3_internal_cfg = load_s3_internal()
rabbit_cfg = RabbitMQConfig()
SessionLocal = None
_purge_thread_started = False

INTERNAL_PURGE_INTERVAL_SECONDS = max(60, int(os.getenv("INTERNAL_PURGE_INTERVAL_SECONDS", "86400")))
INTERNAL_PURGE_MAX_AGE_DAYS = max(1, int(os.getenv("INTERNAL_PURGE_MAX_AGE_DAYS", "7")))
INTERNAL_PURGE_LOCK_ID = int(os.getenv("INTERNAL_PURGE_LOCK_ID", "910019001"))

EXTERNAL_CALLBACK_URL = os.getenv(
    "EXTERNAL_CALLBACK_URL", "http://upload-portal:8081/api/notify-status"
)


def notify_external_status(file_id: str, status: str, message: str, timeout: int = 5) -> None:
    """Push transfer progression/status back to external portal."""
    try:
        resp = req.post(
            EXTERNAL_CALLBACK_URL,
            json={
                "file_id": file_id,
                "status": status,
                "message": message,
            },
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to callback external zone (%s): %s", status, e)


def verify_token():
    auth = request.headers.get("Authorization", "")
    return verify_bearer_token(auth, INTERNAL_API_TOKEN)


def run_internal_purge_once():
    """Purge imported files older than configured age from internal DB/S3."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=INTERNAL_PURGE_MAX_AGE_DAYS)
    db = SessionLocal()
    removed_db = 0
    removed_s3 = 0
    skipped_s3 = 0
    lock_acquired = False
    try:
        lock_acquired = bool(
            db.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": INTERNAL_PURGE_LOCK_ID},
            ).scalar()
        )
        if not lock_acquired:
            logger.debug("Internal purge skipped (lock busy)")
            return

        stale_files = (
            db.query(UserAudioFile)
            .filter(UserAudioFile.created_at < cutoff)
            .all()
        )

        for audio_file in stale_files:
            try:
                delete_object(s3_internal_cfg, audio_file.stored_filename)
                removed_s3 += 1
            except Exception:
                skipped_s3 += 1
                logger.warning("Failed to delete internal object: %s", audio_file.stored_filename)

            db.delete(audio_file)
            removed_db += 1

        db.commit()
        if removed_db:
            logger.info(
                "Internal purge done: db=%d, s3_deleted=%d, s3_failed=%d, cutoff=%s",
                removed_db, removed_s3, skipped_s3, cutoff.isoformat()
            )
    except Exception:
        db.rollback()
        logger.exception("Internal purge failed")
    finally:
        if lock_acquired:
            try:
                db.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": INTERNAL_PURGE_LOCK_ID},
                )
                db.commit()
            except Exception:
                db.rollback()
                logger.warning("Failed to release internal purge lock")
        db.close()


def _purge_loop():
    logger.info(
        "Starting internal purge loop: interval=%ss, max_age=%sd",
        INTERNAL_PURGE_INTERVAL_SECONDS,
        INTERNAL_PURGE_MAX_AGE_DAYS,
    )
    while True:
        run_internal_purge_once()
        time.sleep(INTERNAL_PURGE_INTERVAL_SECONDS)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "zone": "internal"})


@app.route("/api/v1/pull", methods=["POST"])
def pull_file():
    """
    Receive notification from external zone and PULL the file.
    JSON body contains metadata only (not the file).
    """
    if not verify_token():
        logger.warning("Unauthorized pull request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    required = ["file_id", "user_sub", "simple_code", "transcoded_filename"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"error": f"Missing fields: {missing}"}), 400

    file_id = data["file_id"]
    user_sub = data["user_sub"]
    transcoded_filename = data["transcoded_filename"]
    simple_code = data["simple_code"]
    internal_key = f"{user_sub}/{simple_code}/{transcoded_filename}"

    logger.info("Pull request: file_id=%s, user=%s, file=%s", file_id, user_sub, transcoded_filename)

    try:
        # Idempotency guard: if already imported, acknowledge success and stop.
        db = SessionLocal()
        try:
            existing = (
                db.query(UserAudioFile)
                .filter(UserAudioFile.stored_filename == internal_key)
                .first()
            )
        finally:
            db.close()
        if existing:
            logger.info("Idempotent replay detected for %s, key already present: %s", file_id, internal_key)
            notify_external_status(
                file_id,
                "transferred",
                "Fichier déjà intégré (idempotence). Transcription en cours... (100%)",
            )
            return jsonify({
                "status": "already_pulled",
                "file_id": file_id,
                "internal_key": internal_key,
            })

        # ── PULL the file from processed-staging S3 ──
        notify_external_status(file_id, "transferring", "Transfert: téléchargement depuis la zone de transit (45%)")
        logger.info("Pulling from processed-staging: %s", transcoded_filename)
        file_data = download_fileobj(s3_processed_cfg, transcoded_filename)
        file_size = file_data.getbuffer().nbytes

        # ── Store in internal S3 under user directory ──
        notify_external_status(file_id, "transferring", "Transfert: copie vers la zone interne (70%)")
        upload_fileobj(s3_internal_cfg, internal_key, file_data, "audio/wav")
        logger.info("Stored internally: %s (%d bytes)", internal_key, file_size)

        # ── Create internal DB record ──
        notify_external_status(file_id, "transferring", "Transfert: finalisation et indexation (90%)")
        db = SessionLocal()
        try:
            audio_file = UserAudioFile(
                id=uuid4(),
                user_sub=user_sub,
                user_email=data.get("user_email"),
                original_session_code=simple_code,
                original_filename=data.get("original_filename", transcoded_filename),
                stored_filename=internal_key,
                file_size_bytes=file_size,
                audio_quality_score=data.get("quality_score"),
                audio_duration_seconds=data.get("duration_seconds"),
                transcription_status="pending",
            )
            db.add(audio_file)
            db.commit()

            # ── Trigger transcription ──
            try:
                publish_message(rabbit_cfg, QUEUE_TRANSCRIPTION, {
                    "audio_file_id": str(audio_file.id),
                    "user_sub": user_sub,
                    "stored_filename": internal_key,
                    "original_filename": data.get("original_filename"),
                    "simple_code": simple_code,
                })
                logger.info("Transcription enqueued for %s", audio_file.id)
            except Exception as e:
                logger.warning("Failed to enqueue transcription: %s", e)

        finally:
            db.close()

        # ── Notify external zone of successful transfer ──
        notify_external_status(file_id, "transferred", "Fichier intégré à votre compte. Transcription en cours... (100%)")

        return jsonify({
            "status": "pulled",
            "file_id": file_id,
            "internal_key": internal_key,
        })

    except Exception:
        logger.exception("Failed to pull file %s", file_id)
        return jsonify({"error": "Internal error during pull"}), 500


def create_app():
    global SessionLocal, _purge_thread_started
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, InternalBase)
    ensure_bucket(s3_internal_cfg)
    try:
        declare_queues(rabbit_cfg)
    except Exception as e:
        logger.warning("Could not declare queues (may be normal if separate RabbitMQ): %s", e)
    SessionLocal = create_session_factory(db_cfg)
    if not _purge_thread_started:
        purge_thread = threading.Thread(target=_purge_loop, daemon=True, name="internal-purge-loop")
        purge_thread.start()
        _purge_thread_started = True
    return app


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port = int(os.getenv("FILE_PULLER_PORT", 8090))
    application.run(host="0.0.0.0", port=port)
