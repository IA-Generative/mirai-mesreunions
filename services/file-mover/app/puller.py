"""
File Puller Service (Zone Interne)
==================================
Entry point that integrates a transcoded file into the protected zone.

Two ways to learn that a new file is ready:
  1. Drain ``internal_pull`` queue periodically (every
     ``INTERNAL_PULL_QUEUE_INTERVAL_SECONDS``). The AMQP socket is opened
     **outbound** from inside the protected zone — no inbound connection
     ever crosses the boundary. This is the source of truth.
  2. ``/api/v1/pull-trigger`` HTTP endpoint exposed via Ingress: an
     optional, bearer-protected wake-up that lets file-mover ask "drain now"
     and reach near-zero latency. If anything blocks the trigger (ACL,
     network, token rotated), the polling tick still catches up.

Both paths converge on ``_perform_pull(payload)``, which does the actual
S3 download/upload, DB insert, and transcription enqueue. The function is
idempotent: calling it twice with the same ``(user_sub, simple_code,
transcoded_filename)`` tuple results in a single internal record.

The legacy ``/api/v1/pull`` route is preserved for backward compatibility
with deployments where in-cluster DNS still works (local docker-compose,
single-cluster integrations). It calls ``_perform_pull`` directly with the
HTTP body — same logic, just a different trigger.
"""

import ipaddress
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from io import BytesIO
from typing import Optional
from uuid import uuid4
from pathlib import Path

import requests as req
from flask import Flask, request, jsonify
from sqlalchemy import text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_int_db, load_s3_processed, load_s3_internal,
    INTERNAL_API_TOKEN, RabbitMQConfig,
    INTERNAL_PULL_QUEUE_INTERVAL_SECONDS,
    MCR_PUSH_ENABLED, MCR_GATEWAY_URL, OIDC_TOKEN_ENDPOINT,
)
from libs.shared.app.models import InternalBase, UserAudioFile
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.s3_helper import download_fileobj, upload_fileobj, ensure_bucket, delete_object
from libs.shared.app.queue_helper import (
    publish_message,
    declare_queues,
    drain_queue_once,
    QUEUE_TRANSCRIPTION,
    QUEUE_INTERNAL_PULL,
)
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.secrets_crypto import decrypt as decrypt_secret
from libs.shared.app.oidc_refresh_store import fetch_ciphertext, delete_ciphertext
from app.mcr_client import (
    MCRClient,
    MCRAuthError,
    MCRTransientError,
    MCRApplicativeError,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

db_cfg = load_int_db()
s3_processed_cfg = load_s3_processed()
s3_internal_cfg = load_s3_internal()
rabbit_cfg = RabbitMQConfig()
SessionLocal = None
_purge_thread_started = False
_pull_loop_thread_started = False

INTERNAL_PURGE_INTERVAL_SECONDS = max(60, int(os.getenv("INTERNAL_PURGE_INTERVAL_SECONDS", "86400")))
INTERNAL_PURGE_MAX_AGE_DAYS = max(1, int(os.getenv("INTERNAL_PURGE_MAX_AGE_DAYS", "7")))
INTERNAL_PURGE_LOCK_ID = int(os.getenv("INTERNAL_PURGE_LOCK_ID", "910019001"))

EXTERNAL_CALLBACK_URL = os.getenv(
    "EXTERNAL_CALLBACK_URL", "http://upload-portal:8081/api/notify-status"
)

INTERNAL_PUSH_TRIGGER_TOKEN = os.getenv("INTERNAL_PUSH_TRIGGER_TOKEN", "")
_TRIGGER_IP_ALLOWLIST_RAW = os.getenv("INTERNAL_PUSH_TRIGGER_IP_ALLOWLIST", "")


def _parse_ip_allowlist(raw: str):
    """Return a list of ip_network objects, ignoring blank entries."""
    nets = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid CIDR in trigger allowlist: %s", token)
    return nets


_TRIGGER_ALLOWED_NETS = _parse_ip_allowlist(_TRIGGER_IP_ALLOWLIST_RAW)


def _client_ip_allowed(remote_addr: str) -> bool:
    """Empty allowlist => allow (rely on nginx whitelist + bearer)."""
    if not _TRIGGER_ALLOWED_NETS:
        return True
    try:
        client = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(client in net for net in _TRIGGER_ALLOWED_NETS)


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


def verify_trigger_token():
    """Bearer for the new /api/v1/pull-trigger route — distinct from INTERNAL_API_TOKEN."""
    auth = request.headers.get("Authorization", "")
    return bool(INTERNAL_PUSH_TRIGGER_TOKEN) and verify_bearer_token(auth, INTERNAL_PUSH_TRIGGER_TOKEN)


def _guess_audio_mime(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext == ".mp4":
        return "audio/mp4"
    if ext == ".m4a":
        return "audio/mp4"
    if ext == ".wav":
        return "audio/wav"
    if ext == ".ogg":
        return "audio/ogg"
    return "application/octet-stream"


def _build_mcr_client() -> Optional[MCRClient]:
    """Construct the MCR client lazily, returning None if disabled or misconfigured."""
    if not MCR_PUSH_ENABLED:
        return None
    try:
        return MCRClient(
            gateway_url=MCR_GATEWAY_URL,
            oidc_token_endpoint=OIDC_TOKEN_ENDPOINT,
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
        )
    except ValueError:
        logger.exception(
            "MCR_PUSH_ENABLED but MCR client config is incomplete; falling back to local transcription queue"
        )
        return None


def _set_user_audio_status(audio_file_id, status: str, mcr_meeting_id: Optional[str] = None) -> None:
    """Update transcription_status (and optionally mcr_meeting_id) for an audio file."""
    db = SessionLocal()
    try:
        rec = db.query(UserAudioFile).filter(UserAudioFile.id == audio_file_id).first()
        if rec is None:
            logger.warning("UserAudioFile not found for status update: %s", audio_file_id)
            return
        rec.transcription_status = status
        if mcr_meeting_id is not None:
            rec.mcr_meeting_id = mcr_meeting_id
        db.commit()
    finally:
        db.close()


def _push_to_mcr(audio_file_id, user_sub: str, transcoded_filename: str,
                 file_data, payload: dict) -> None:
    """
    Asynchronously push a file to MCR for transcription. The 4-step sequence
    (refresh exchange → create meeting → presigned URL → PUT binary) is
    classified into 3 error families:

      - MCRAuthError        : refresh expired/revoked. Wipe stored token,
                              mark mcr_auth_failed, NO retry.
      - MCRApplicativeError : 4xx applicative. Mark mcr_rejected, NO retry.
      - MCRTransientError   : 5xx / network. Re-raised so the queue
                              consumer's retry counter handles it.
    """
    client = _build_mcr_client()
    if client is None:
        # Misconfigured but enabled — preserve the file in mcr_push_failed so
        # ops sees something concrete in the dashboard rather than a silent skip.
        _set_user_audio_status(audio_file_id, "mcr_push_failed")
        return

    ciphertext = fetch_ciphertext(user_sub)
    if not ciphertext:
        logger.warning("MCR push: no refresh token stored for user_sub=%s; user must re-login", user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return

    try:
        refresh_token = decrypt_secret(ciphertext)
    except Exception:
        logger.exception("MCR push: failed to decrypt refresh token for user_sub=%s", user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return

    # Step 1: refresh → access
    try:
        access_token = client.exchange_refresh(refresh_token)
    except MCRAuthError:
        logger.warning("MCR push: refresh rejected by KC for user_sub=%s; deleting stored token", user_sub)
        delete_ciphertext(user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return

    # Steps 2-4: create meeting → presigned → PUT
    meeting_payload = {
        "name": payload.get("original_filename", transcoded_filename),
        "name_platform": "IMPORT",
    }
    try:
        meeting_id = client.create_meeting(access_token, meeting_payload)
        presigned = client.generate_presigned(access_token, meeting_id, transcoded_filename)
        # file_data is a BytesIO already loaded in RAM from the audio-internal upload step.
        # Reset position and read raw bytes for the PUT.
        file_data.seek(0)
        body = file_data.read()
        client.upload_binary(presigned, body, _guess_audio_mime(transcoded_filename))
    except MCRAuthError:
        # Token was accepted at exchange but rejected on /meetings — likely stale
        # KC revocation between calls. Treat as auth failure.
        logger.warning("MCR push: meeting/presigned/upload rejected with auth error for %s", audio_file_id)
        delete_ciphertext(user_sub)
        _set_user_audio_status(audio_file_id, "mcr_auth_failed")
        return
    except MCRApplicativeError:
        logger.exception("MCR push: applicative error for %s, marking mcr_rejected", audio_file_id)
        _set_user_audio_status(audio_file_id, "mcr_rejected")
        return
    # MCRTransientError propagates → consumer retries via x-retry-count

    _set_user_audio_status(audio_file_id, "mcr_pushed", mcr_meeting_id=meeting_id)
    logger.info("MCR push success: audio_file_id=%s meeting_id=%s", audio_file_id, meeting_id)


def _perform_pull(payload: dict) -> dict:
    """
    Pull a transcoded file from processed-staging into the internal zone.

    Idempotent: if a UserAudioFile already exists for this internal_key, the
    function returns ``status=already_pulled`` without re-downloading.

    Raises on infrastructure errors (S3 unreachable, DB down) so the caller
    (queue drain or HTTP handler) can decide whether to retry. Returns a
    dict on success suitable for JSON response.
    """
    required = ("file_id", "user_sub", "simple_code", "transcoded_filename")
    missing = [f for f in required if f not in payload]
    if missing:
        raise ValueError(f"Missing fields: {missing}")

    file_id = payload["file_id"]
    user_sub = payload["user_sub"]
    transcoded_filename = payload["transcoded_filename"]
    simple_code = payload["simple_code"]
    auto_transcribe = bool(payload.get("auto_transcribe", True))
    internal_key = f"{user_sub}/{simple_code}/{transcoded_filename}"

    logger.info("Pull request: file_id=%s, user=%s, file=%s", file_id, user_sub, transcoded_filename)

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
        return {"status": "already_pulled", "file_id": file_id, "internal_key": internal_key}

    notify_external_status(file_id, "transferring", "Transfert: téléchargement depuis la zone de transit (45%)")
    logger.info("Pulling from processed-staging: %s", transcoded_filename)
    file_data = download_fileobj(s3_processed_cfg, transcoded_filename)
    file_size = file_data.getbuffer().nbytes

    notify_external_status(file_id, "transferring", "Transfert: copie vers la zone interne (70%)")
    upload_fileobj(s3_internal_cfg, internal_key, file_data, _guess_audio_mime(transcoded_filename))
    logger.info("Stored internally: %s (%d bytes)", internal_key, file_size)

    notify_external_status(file_id, "transferring", "Transfert: finalisation et indexation (90%)")
    db = SessionLocal()
    try:
        audio_file = UserAudioFile(
            id=uuid4(),
            user_sub=user_sub,
            user_email=payload.get("user_email"),
            original_session_code=simple_code,
            original_filename=payload.get("original_filename", transcoded_filename),
            stored_filename=internal_key,
            file_size_bytes=file_size,
            audio_quality_score=payload.get("quality_score"),
            audio_duration_seconds=payload.get("duration_seconds"),
            transcription_status="pending" if auto_transcribe else "disabled",
        )
        db.add(audio_file)
        db.commit()
        audio_file_id = str(audio_file.id)

        if auto_transcribe:
            if MCR_PUSH_ENABLED:
                # Push directly to the MCR platform instead of using the local
                # transcription-stub queue. _push_to_mcr re-uses the file_data
                # already loaded in RAM from the audio-internal upload step
                # above, so we don't re-download from S3.
                _push_to_mcr(
                    audio_file_id=audio_file.id,
                    user_sub=user_sub,
                    transcoded_filename=transcoded_filename,
                    file_data=file_data,
                    payload=payload,
                )
            else:
                try:
                    publish_message(rabbit_cfg, QUEUE_TRANSCRIPTION, {
                        "audio_file_id": audio_file_id,
                        "user_sub": user_sub,
                        "stored_filename": internal_key,
                        "original_filename": payload.get("original_filename"),
                        "simple_code": simple_code,
                    })
                    logger.info("Transcription enqueued for %s", audio_file_id)
                except Exception as e:
                    logger.warning("Failed to enqueue transcription: %s", e)
        else:
            logger.info("Transcription disabled by token flag for %s", audio_file_id)

    finally:
        db.close()

    if auto_transcribe:
        notify_external_status(file_id, "transferred", "Fichier intégré à votre compte. Transcription en cours... (100%)")
    else:
        notify_external_status(file_id, "transferred", "Fichier intégré à votre compte. Transcription automatique désactivée pour ce code. (100%)")

    return {"status": "pulled", "file_id": file_id, "internal_key": internal_key}


def _drain_internal_pull_callback(message: dict) -> bool:
    """Adapter for queue drain: True ⇒ ack, False/exception ⇒ retry counter."""
    try:
        _perform_pull(message)
        return True
    except ValueError:
        # Bad payload — don't retry, just log and drop via the helper's drop path.
        logger.exception("Invalid internal_pull payload, dropping: %s", message)
        return True
    except Exception:
        logger.exception("internal_pull processing failed, will retry via queue counter")
        return False


def _drain_internal_pull_queue() -> int:
    """Drain the internal_pull queue once. Safe to call from anywhere."""
    return drain_queue_once(rabbit_cfg, QUEUE_INTERNAL_PULL, _drain_internal_pull_callback)


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


def _pull_queue_loop():
    """Poll-drain internal_pull at the configured cadence."""
    logger.info(
        "Starting internal_pull drain loop: interval=%ss",
        INTERNAL_PULL_QUEUE_INTERVAL_SECONDS,
    )
    while True:
        try:
            handled = _drain_internal_pull_queue()
            if handled:
                logger.info("Drained %d message(s) from internal_pull", handled)
        except Exception:
            logger.exception("Drain loop iteration failed; will retry next tick")
        time.sleep(INTERNAL_PULL_QUEUE_INTERVAL_SECONDS)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "zone": "internal"})


@app.route("/healthz")
def healthz():
    return health()


@app.route("/api/v1/pull", methods=["POST"])
def pull_file():
    """
    Legacy endpoint preserved for backward compatibility with single-cluster
    deployments where in-cluster DNS resolves between zones (e.g.
    docker-compose, integration). In prod-bêta this route is unreachable
    from the DMZ; the trigger goes through /api/v1/pull-trigger instead.
    """
    if not verify_token():
        logger.warning("Unauthorized pull request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    try:
        result = _perform_pull(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("Failed to pull file %s", data.get("file_id"))
        return jsonify({"error": "Internal error during pull"}), 500
    return jsonify(result)


@app.route("/api/v1/pull-trigger", methods=["POST"])
def pull_trigger():
    """
    Optional cross-cluster wake-up. Bearer-protected with
    ``INTERNAL_PUSH_TRIGGER_TOKEN`` (distinct from INTERNAL_API_TOKEN so the
    two can be rotated independently). The body is ignored — invocation is
    a pure "drain now" signal. The actual messages live on the
    ``internal_pull`` AMQP queue and are the source of truth.
    """
    if not verify_trigger_token():
        logger.warning("Unauthorized trigger request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401
    if not _client_ip_allowed(request.remote_addr or ""):
        logger.warning("Trigger IP not in allowlist: %s", request.remote_addr)
        return jsonify({"error": "Forbidden"}), 403
    try:
        handled = _drain_internal_pull_queue()
    except Exception:
        logger.exception("Trigger-driven drain failed")
        return jsonify({"error": "drain_failed"}), 500
    return jsonify({"status": "ok", "drained": handled})


def create_app():
    global SessionLocal, _purge_thread_started, _pull_loop_thread_started
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
    if not _pull_loop_thread_started:
        pull_thread = threading.Thread(target=_pull_queue_loop, daemon=True, name="internal-pull-drain")
        pull_thread.start()
        _pull_loop_thread_started = True
    if INTERNAL_PUSH_TRIGGER_TOKEN:
        logger.info("Pull trigger HTTP endpoint enabled (allowlist=%s)",
                    _TRIGGER_IP_ALLOWLIST_RAW or "<empty>")
    else:
        logger.info("Pull trigger HTTP endpoint disabled (no INTERNAL_PUSH_TRIGGER_TOKEN set)")
    return app


# WSGI entrypoint for Gunicorn. Skipped in tests via SKIP_CREATE_APP=1 so unit
# tests can import this module without spinning up DB/S3/RabbitMQ connections.
if os.getenv("SKIP_CREATE_APP", "0") != "1":
    application = create_app()
else:
    application = app


if __name__ == "__main__":
    port = int(os.getenv("FILE_PULLER_PORT", 8090))
    application.run(host="0.0.0.0", port=port)
