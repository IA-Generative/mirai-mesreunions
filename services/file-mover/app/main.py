"""
File Mover Service (Zone Externe)
=================================
Consumes from the file-ready queue and NOTIFIES the internal zone
that a file is ready to be pulled.

CRITICAL SECURITY: This service NEVER pushes files to the internal zone.
It only sends a notification (file metadata) via API. The internal zone's
File Puller then initiates the data transfer (PULL pattern).
"""

import logging
import os
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_ext_db, RabbitMQConfig, INTERNAL_API_URL, INTERNAL_API_TOKEN,
)
from libs.shared.app.models import ExternalBase, UploadedFile, UploadSession, UploadStatus
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.queue_helper import consume_queue, declare_queues, QUEUE_FILE_READY, RabbitMQConfig
from libs.shared.app.security import require_strong_shared_secret

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

db_cfg = load_ext_db()
rabbit_cfg = RabbitMQConfig()

UPLOAD_PORTAL_URL = os.getenv("UPLOAD_PORTAL_INTERNAL_URL", "http://upload-portal:8081")
PULL_REQUEST_TIMEOUT_SECONDS = max(15, int(os.getenv("PULL_REQUEST_TIMEOUT_SECONDS", "90")))


def notify_portal(session_obj, file_obj, status_msg):
    """Notify the upload portal of a status change."""
    try:
        requests.post(f"{UPLOAD_PORTAL_URL}/api/notify-status", json={
            "qr_token": session_obj.qr_token if session_obj else None,
            "file_id": str(file_obj.id),
            "filename": file_obj.original_filename,
            "status": file_obj.status.value,
            "message": status_msg,
            "quality": file_obj.audio_quality_score,
        }, headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        }, timeout=5)
    except Exception as e:
        logger.warning("Failed to notify portal: %s", e)


def notify_internal_zone(message: dict) -> bool:
    """
    Send a NOTIFICATION (not the file) to the internal zone's File Puller.
    The internal zone will then PULL the file from processed-staging S3.
    """
    payload = {
        "file_id": message["file_id"],
        "session_id": message["session_id"],
        "user_sub": message["user_sub"],
        "user_email": message.get("user_email"),
        "simple_code": message["simple_code"],
        "original_filename": message["original_filename"],
        "transcoded_filename": message["transcoded_filename"],
        "quality_score": message.get("quality_score"),
        "duration_seconds": message.get("duration_seconds"),
    }

    try:
        resp = requests.post(
            INTERNAL_API_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=PULL_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        logger.info("Internal zone notified for file %s", message["file_id"])
        return True
    except requests.RequestException as e:
        logger.error("Failed to notify internal zone: %s", e)
        return False


def process_file_ready(message: dict) -> bool:
    """Process a file-ready notification."""
    file_id = message["file_id"]
    logger.info("File ready for transfer: %s", file_id)

    SessionLocal = create_session_factory(db_cfg)
    db = SessionLocal()

    try:
        file_obj = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
        if not file_obj:
            logger.error("File not found: %s", file_id)
            return True

        session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()

        # Mark as ready for transfer
        file_obj.status = UploadStatus.READY_FOR_TRANSFER
        file_obj.status_message = "Prêt pour le transfert vers la zone sécurisée..."
        db.commit()
        notify_portal(session_obj, file_obj, file_obj.status_message)

        # Notify internal zone (PULL pattern - only metadata, not the file)
        success = notify_internal_zone(message)

        if success:
            file_obj.status = UploadStatus.TRANSFERRING
            file_obj.status_message = "Transfert démarré côté interne (20%)"
            db.commit()
            notify_portal(session_obj, file_obj, file_obj.status_message)
        else:
            # Will be retried by RabbitMQ
            logger.warning("Internal notification failed, will retry")
            return False

        return True

    except Exception:
        logger.exception("Error processing file-ready for %s", file_id)
        return False
    finally:
        db.close()


def main():
    logger.info("Starting File Mover (external zone notifier)...")
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    declare_queues(rabbit_cfg)
    consume_queue(rabbit_cfg, QUEUE_FILE_READY, process_file_ready)


if __name__ == "__main__":
    main()
