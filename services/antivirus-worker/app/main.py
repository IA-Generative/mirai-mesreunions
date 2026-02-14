"""
Antivirus Worker
================
Consumes files from the AV scan queue, scans with ClamAV,
updates status, and routes to transcode queue or quarantine.
"""

import logging
import os
import sys
import tempfile
from datetime import datetime, timezone

import clamd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import load_ext_db, load_s3_upload, RabbitMQConfig, INTERNAL_API_TOKEN
from libs.shared.app.models import ExternalBase, UploadedFile, UploadSession, UploadStatus
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.s3_helper import download_fileobj
from libs.shared.app.queue_helper import (
    consume_queue, publish_message, declare_queues,
    QUEUE_AV_SCAN, QUEUE_TRANSCODE, RabbitMQConfig,
)
from libs.shared.app.security import require_strong_shared_secret

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

db_cfg = load_ext_db()
s3_cfg = load_s3_upload()
rabbit_cfg = RabbitMQConfig()

CLAMAV_HOST = os.getenv("CLAMAV_HOST", "clamav")
CLAMAV_PORT = int(os.getenv("CLAMAV_PORT", 3310))
UPLOAD_PORTAL_URL = os.getenv("UPLOAD_PORTAL_INTERNAL_URL", "http://upload-portal:8081")


def notify_portal(session_obj, file_obj, status_msg):
    """Notify the upload portal of a status change via HTTP."""
    import requests
    try:
        requests.post(f"{UPLOAD_PORTAL_URL}/api/notify-status", json={
            "qr_token": session_obj.qr_token if session_obj else None,
            "file_id": str(file_obj.id),
            "filename": file_obj.original_filename,
            "status": file_obj.status.value,
            "message": status_msg,
        }, headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        }, timeout=5)
    except Exception as e:
        logger.warning("Failed to notify portal: %s", e)


def get_clamav_client():
    """Connect to ClamAV daemon."""
    cd = clamd.ClamdNetworkSocket(host=CLAMAV_HOST, port=CLAMAV_PORT, timeout=120)
    cd.ping()
    return cd


def process_av_scan(message: dict) -> bool:
    """Process an antivirus scan job."""
    file_id = message["file_id"]
    stored_filename = message["stored_filename"]

    logger.info("Scanning file: %s (id=%s)", stored_filename, file_id)

    SessionLocal = create_session_factory(db_cfg)
    db = SessionLocal()

    try:
        file_obj = db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
        if not file_obj:
            logger.error("File not found: %s", file_id)
            return True  # ack to avoid requeue

        session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()

        # Update status to scanning
        file_obj.status = UploadStatus.SCANNING
        file_obj.status_message = "Analyse antivirale en cours..."
        db.commit()
        notify_portal(session_obj, file_obj, file_obj.status_message)

        # Download file from S3
        file_data = download_fileobj(s3_cfg, stored_filename)

        # Scan with ClamAV
        try:
            cd = get_clamav_client()
            result = cd.instream(file_data)
            scan_status = result.get("stream", ("OK", ""))[0]
            scan_detail = result.get("stream", ("", ""))[1]
        except Exception as e:
            logger.exception("ClamAV scan failed")
            file_obj.status = UploadStatus.ERROR
            file_obj.status_message = "Erreur lors de l'analyse antivirale."
            db.commit()
            notify_portal(session_obj, file_obj, file_obj.status_message)
            return False  # retry

        file_obj.av_scanned_at = datetime.now(timezone.utc)
        file_obj.av_result = f"{scan_status}: {scan_detail}" if scan_detail else scan_status

        if scan_status == "OK":
            # Clean file → send to transcode queue
            file_obj.status = UploadStatus.SCAN_CLEAN
            file_obj.status_message = "Fichier sain. Transcodage en cours..."
            db.commit()
            notify_portal(session_obj, file_obj, file_obj.status_message)

            publish_message(rabbit_cfg, QUEUE_TRANSCODE, {
                **message,
                "av_result": "clean",
            })
            logger.info("File %s is clean, sent to transcode queue", file_id)
        else:
            # Infected → quarantine
            file_obj.status = UploadStatus.QUARANTINED
            file_obj.status_message = f"⚠️ Virus détecté ({scan_detail}). Fichier mis en quarantaine."
            db.commit()
            notify_portal(session_obj, file_obj, file_obj.status_message)
            logger.warning("File %s INFECTED: %s", file_id, scan_detail)

        return True

    except Exception:
        logger.exception("Error processing AV scan for %s", file_id)
        return False
    finally:
        db.close()


def main():
    logger.info("Starting Antivirus Worker...")
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    declare_queues(rabbit_cfg)
    consume_queue(rabbit_cfg, QUEUE_AV_SCAN, process_av_scan)


if __name__ == "__main__":
    main()
