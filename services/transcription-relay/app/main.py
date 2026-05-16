"""
Transcription Stub Service (Zone Interne)
==========================================
Simulates Speech-to-Text API calls for transcription.
Consumes from the transcription queue and updates the internal DB.
In production, replace the stub with real STT API calls.
"""

import logging
import os
import sys
import time
import random
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import load_int_db, RabbitMQConfig
from libs.shared.app.models import InternalBase, UserAudioFile, TranscriptionEvent
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.queue_helper import consume_queue, declare_queues, QUEUE_TRANSCRIPTION

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

db_cfg = load_int_db()
rabbit_cfg = RabbitMQConfig()

SIMULATE_DELAY = int(os.getenv("TRANSCRIPTION_SIMULATE_DELAY_SECONDS", 5))

# Simulated transcription results for testing
SAMPLE_TRANSCRIPTIONS = [
    "Bonjour, je souhaite vous transmettre un enregistrement vocal concernant le dossier numéro 2024-0537.",
    "Suite à notre conversation téléphonique, voici les éléments complémentaires que je souhaitais vous communiquer.",
    "Ce message vocal contient les observations du terrain effectuées lors de la visite du site le 15 janvier.",
    "Comme convenu lors de la réunion, je vous transmets mes commentaires sur le projet de réorganisation.",
    "Message urgent concernant la mise à jour du système informatique prévue pour la semaine prochaine.",
]


def simulate_stt_api_call(audio_file_id: str, stored_filename: str) -> dict:
    """
    Simulates a Speech-to-Text API call.
    In production, this would call Whisper, Google STT, Azure Speech, etc.

    Returns: {
        "success": bool,
        "text": str | None,
        "confidence": float,
        "language": str,
    }
    """
    logger.info("Simulating STT API call for %s...", audio_file_id)

    # Simulate API latency
    time.sleep(SIMULATE_DELAY)

    # Simulate occasional failures (5% rate)
    if random.random() < 0.05:
        return {
            "success": False,
            "text": None,
            "confidence": 0,
            "language": "unknown",
            "error": "STT API timeout (simulated)",
        }

    return {
        "success": True,
        "text": random.choice(SAMPLE_TRANSCRIPTIONS),
        "confidence": round(random.uniform(0.75, 0.99), 2),
        "language": "fr",
    }


def process_transcription(message: dict) -> bool:
    """Process a transcription job."""
    audio_file_id = message["audio_file_id"]
    stored_filename = message.get("stored_filename", "")

    logger.info("Starting transcription for %s", audio_file_id)

    SessionLocal = create_session_factory(db_cfg)
    db = SessionLocal()

    try:
        audio_file = db.query(UserAudioFile).filter(UserAudioFile.id == audio_file_id).first()
        if not audio_file:
            logger.error("Audio file not found: %s", audio_file_id)
            return True

        def log_event(event_type: str, message_txt: str = "", metadata: dict | None = None):
            event = TranscriptionEvent(
                audio_file_id=audio_file.id,
                original_session_code=audio_file.original_session_code,
                event_type=event_type,
                message=message_txt,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=True),
            )
            db.add(event)
            db.commit()

        # Update status
        audio_file.transcription_status = "processing"
        audio_file.transcription_started_at = datetime.now(timezone.utc)
        db.commit()
        log_event(
            "transcription_started",
            "Transcription job started by stub",
            {"stored_filename": stored_filename},
        )

        # Call STT API (simulated)
        started_at = time.time()
        log_event("stt_api_call_started", "Calling simulated STT API", {})
        result = simulate_stt_api_call(audio_file_id, stored_filename)
        duration_ms = int((time.time() - started_at) * 1000)

        if result["success"]:
            audio_file.transcription_status = "completed"
            audio_file.transcription_text = result["text"]
            audio_file.transcription_completed_at = datetime.now(timezone.utc)
            logger.info(
                "Transcription completed for %s (confidence=%.2f, lang=%s)",
                audio_file_id, result["confidence"], result["language"],
            )
            log_event(
                "stt_api_call_succeeded",
                "Simulated STT API returned success",
                {
                    "duration_ms": duration_ms,
                    "confidence": result.get("confidence"),
                    "language": result.get("language"),
                    "text_preview": (result.get("text") or "")[:120],
                },
            )
            log_event("transcription_completed", "Transcription completed", {})
        else:
            audio_file.transcription_status = "failed"
            audio_file.transcription_text = None
            logger.warning("Transcription failed for %s: %s", audio_file_id, result.get("error"))
            log_event(
                "stt_api_call_failed",
                "Simulated STT API returned failure",
                {
                    "duration_ms": duration_ms,
                    "error": result.get("error", "unknown error"),
                },
            )
            log_event("transcription_failed", "Transcription failed", {"error": result.get("error", "")})

        db.commit()
        return True

    except Exception as exc:
        logger.exception("Error processing transcription for %s", audio_file_id)
        try:
            db.add(
                TranscriptionEvent(
                    audio_file_id=audio_file_id,
                    original_session_code=message.get("simple_code"),
                    event_type="transcription_exception",
                    message="Unhandled exception in transcription stub",
                    metadata_json=json.dumps({"error": str(exc)}, ensure_ascii=True),
                )
            )
            db.commit()
        except Exception:
            logger.exception("Failed to persist transcription_exception event for %s", audio_file_id)
        return False
    finally:
        db.close()


def main():
    logger.info("Starting Transcription Stub Worker (zone interne)...")
    init_tables(db_cfg, InternalBase)
    try:
        declare_queues(rabbit_cfg)
    except Exception as e:
        logger.warning("Queue declaration issue: %s", e)
    consume_queue(rabbit_cfg, QUEUE_TRANSCRIPTION, process_transcription)


if __name__ == "__main__":
    main()
