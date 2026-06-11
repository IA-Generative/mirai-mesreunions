"""
File Mover Service (Zone Externe)
=================================
Consumes from the file-ready queue and notifies the internal zone that a
file is ready to be pulled. The notification is a durable AMQP message on
the `internal_pull` queue (the source of truth) plus an optional best-effort
HTTP trigger to wake the puller up immediately.

CRITICAL SECURITY: this service NEVER pushes file content into the internal
zone. The notification carries metadata only; the internal zone's File
Puller is responsible for downloading the file from S3 once it sees the
message — that "PULL pattern" is what justifies the cross-zone trust break.
"""

import logging
import os
import sys
from typing import Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_ext_db,
    RabbitMQConfig,
    INTERNAL_API_TOKEN,
    INTERNAL_PUSH_TRIGGER_URL,
)
from libs.shared.app.models import (
    ExternalBase,
    UploadedFile,
    UploadSession,
    UploadStatus,
    UploadTokenOption,
)
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.queue_helper import (
    consume_queue,
    declare_queues,
    publish_message,
    QUEUE_FILE_READY,
    QUEUE_INTERNAL_PULL,
)
from libs.shared.app.security import require_strong_shared_secret, resolve_auto_transcribe
from libs.shared.app.trigger_url import resolved_trigger_url

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

db_cfg = load_ext_db()
rabbit_cfg = RabbitMQConfig()

UPLOAD_PORTAL_URL = os.getenv("UPLOAD_PORTAL_INTERNAL_URL", "http://mobile-upload-pwa:8081")
PULL_TRIGGER_HTTP_TIMEOUT_SECONDS = max(1, int(os.getenv("PULL_TRIGGER_HTTP_TIMEOUT_SECONDS", "3")))
INTERNAL_PUSH_TRIGGER_TOKEN = os.getenv("INTERNAL_PUSH_TRIGGER_TOKEN", "")

# Resolved once at import time so the boot log is unambiguous. Tests can
# override by re-reading the env var via _resolve_trigger_url_from_env().
_TRIGGER_URL: Optional[str] = resolved_trigger_url(INTERNAL_PUSH_TRIGGER_URL)


def _resolve_trigger_url_from_env() -> Optional[str]:
    """Helper for tests to re-read the env var after monkey-patching."""
    return resolved_trigger_url(os.getenv("INTERNAL_PUSH_TRIGGER_URL", ""))


def notify_portal(session_obj, file_obj, status_msg):
    """Notify the upload portal of a status change."""
    try:
        requests.post(
            f"{UPLOAD_PORTAL_URL}/api/notify-status",
            json={
                "qr_token": session_obj.qr_token if session_obj else None,
                "file_id": str(file_obj.id),
                "filename": file_obj.original_filename,
                "status": file_obj.status.value,
                "message": status_msg,
                "quality": file_obj.audio_quality_score,
            },
            headers={
                "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=5,
        )
    except Exception as e:
        logger.warning("Failed to notify portal: %s", e)


def _build_pull_payload(message: dict) -> dict:
    return {
        "file_id": message["file_id"],
        "session_id": message["session_id"],
        "user_sub": message["user_sub"],
        "user_email": message.get("user_email"),
        "simple_code": message["simple_code"],
        "original_filename": message["original_filename"],
        "transcoded_filename": message["transcoded_filename"],
        "quality_score": message.get("quality_score"),
        "duration_seconds": message.get("duration_seconds"),
        # Flag absent ⇒ OFF (fail-safe). L'activation est décidée en amont
        # par la politique serveur à l'émission du jeton (cf. PA-01).
        "auto_transcribe": bool(message.get("auto_transcribe", False)),
    }


def _send_http_trigger(payload: dict) -> bool:
    """Best-effort wake-up POST. Never raises — caller is told only via bool."""
    if _TRIGGER_URL is None:
        return False
    headers = {"Content-Type": "application/json"}
    if INTERNAL_PUSH_TRIGGER_TOKEN:
        headers["Authorization"] = f"Bearer {INTERNAL_PUSH_TRIGGER_TOKEN}"
    try:
        resp = requests.post(
            _TRIGGER_URL,
            json={"file_id": payload.get("file_id")},
            headers=headers,
            timeout=PULL_TRIGGER_HTTP_TIMEOUT_SECONDS,
        )
        if resp.status_code >= 400:
            logger.warning(
                "Pull trigger HTTP returned %s for file_id=%s; queue will catch up",
                resp.status_code, payload.get("file_id"),
            )
            return False
        return True
    except Exception as e:
        logger.warning(
            "Pull trigger HTTP call failed (%s); queue will catch up", e,
        )
        return False


def publish_internal_pull(message: dict) -> bool:
    """
    Publish a durable AMQP notification on internal_pull, then optionally
    fire the HTTP wake-up. The AMQP publish is the success criterion: if it
    succeeds, the message is guaranteed to be processed eventually (via
    polling at worst); the HTTP call is just a latency optimisation.
    """
    payload = _build_pull_payload(message)
    try:
        publish_message(rabbit_cfg, QUEUE_INTERNAL_PULL, payload)
    except Exception:
        logger.exception("Failed to publish internal_pull for %s", payload.get("file_id"))
        return False
    _send_http_trigger(payload)  # best-effort, ignore outcome
    return True


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
        token_opt = None
        if session_obj:
            token_opt = (
                db.query(UploadTokenOption)
                .filter(UploadTokenOption.qr_token == session_obj.qr_token)
                .first()
            )

        # Mark as ready for transfer
        file_obj.status = UploadStatus.READY_FOR_TRANSFER
        file_obj.status_message = "Prêt pour le transfert vers la zone sécurisée..."
        db.commit()
        notify_portal(session_obj, file_obj, file_obj.status_message)

        # Notify internal zone via AMQP (and optionally HTTP trigger).
        msg = dict(message)
        # auto_transcribe :
        #   • jeton présent (QR/PWA) ⇒ on respecte la valeur déjà résolue à
        #     l'émission du jeton (device-token-authority a appliqué la policy).
        #   • jeton absent (upload local web : aucune UploadTokenOption) ⇒ on
        #     applique la policy serveur ICI via resolve_auto_transcribe. Le
        #     fail-safe PA-01 est préservé (policy off/absente → False) ; avec
        #     policy "on" les uploads locaux sont enfin transcrits au lieu de
        #     finir "disabled" à vie (incident 2026-06-11).
        if token_opt is not None:
            msg["auto_transcribe"] = bool(token_opt.auto_transcribe)
        else:
            msg["auto_transcribe"] = resolve_auto_transcribe(False)
        published = publish_internal_pull(msg)

        if published:
            file_obj.status = UploadStatus.TRANSFERRING
            file_obj.status_message = "Transfert démarré côté interne (20%)"
            db.commit()
            notify_portal(session_obj, file_obj, file_obj.status_message)
        else:
            logger.warning("Internal_pull publish failed for %s, retrying via queue", file_id)
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
    if _TRIGGER_URL is not None:
        logger.info("Pull HTTP trigger ENABLED towards %s", _TRIGGER_URL)
    else:
        logger.info(
            "Pull HTTP trigger DISABLED (INTERNAL_PUSH_TRIGGER_URL is empty or "
            "not a valid http(s) URL); internal puller will catch up via polling"
        )
    consume_queue(rabbit_cfg, QUEUE_FILE_READY, process_file_ready)


if __name__ == "__main__":
    main()
