"""Async worker — import meetings from MCR (compte-rendu.mirai) into mesreunions.

Consumes ``QUEUE_MCR_IMPORT`` (one message per ``user_audio_files`` row created
by mesreunions-web's ``POST /api/mcr/import``). For each row:

  1. Fetch the user's stored refresh_token (via device-token-authority) and
     exchange it on sso.mirai for a Bearer access_token.
  2. Try ``GET /api/meetings/{id}/audio`` on MCR. On 200, stream the webm
     into the S3 ``audio-internal`` bucket and mark the row ``pending`` so
     the standard transcription pipeline picks it up.
  3. On 404/410 audio AND ``fallback_transcript=true``, fall back to
     ``POST /api/meetings/{id}/transcription`` to fetch a DOCX, extract the
     plain text via python-docx, and persist directly into
     ``transcription_text`` with status ``mcr_transcript_only``.
  4. On any other error path, mark the row ``mcr_import_failed`` so the UI
     can offer a "retry" affordance.

Why this lives in dmz-to-internal-bridge (= internal-ingester) and not in a
separate service : it shares the S3 internal bucket, DB session factory, and
Mirai OIDC machinery already wired up here. Spawning a dedicated container
would add ops surface for a feature that only runs on user demand.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

from libs.shared.app.config import RabbitMQConfig, S3Config, load_s3_internal
from libs.shared.app.mirai_oidc import (
    OIDCApplicativeError,
    OIDCAuthError,
    OIDCTransientError,
    exchange_refresh_token,
)
from libs.shared.app.models import UserAudioFile
from libs.shared.app.oidc_refresh_store import delete_ciphertext, fetch_ciphertext
from libs.shared.app.queue_helper import QUEUE_MCR_IMPORT, consume_queue
from libs.shared.app.s3_helper import upload_fileobj
from libs.shared.app.secrets_crypto import decrypt

from app.mcr_client import (
    MCRApplicativeError,
    MCRAuthError,
    MCRClient,
    MCRTransientError,
)

logger = logging.getLogger("dmz_to_internal_bridge.mcr_importer")


# ─── Configuration ─────────────────────────────────────────────────────

def _mcr_gateway() -> str:
    return os.getenv("MCR_GATEWAY_URL", "").rstrip("/")


def _oidc_token_endpoint() -> str:
    return os.getenv("OIDC_TOKEN_ENDPOINT", "")


def _oidc_client_id() -> str:
    return os.getenv("OIDC_CLIENT_ID", "")


def _oidc_client_secret() -> str:
    return os.getenv("OIDC_CLIENT_SECRET", "")


# ─── Per-message handler ───────────────────────────────────────────────

def _exchange_refresh_for_user(user_sub: str) -> str:
    ciphertext = fetch_ciphertext(user_sub)
    if not ciphertext:
        raise OIDCAuthError("no_refresh_token_stored")
    refresh_token = decrypt(ciphertext)
    try:
        return exchange_refresh_token(
            token_endpoint=_oidc_token_endpoint(),
            client_id=_oidc_client_id(),
            refresh_token=refresh_token,
            client_secret=_oidc_client_secret(),
        )
    except OIDCAuthError:
        # Wipe the bad ciphertext so the next attempt won't retry it.
        try:
            delete_ciphertext(user_sub)
        except Exception:
            logger.exception("delete_ciphertext failed for user_sub=%s", user_sub)
        raise


def _extract_docx_text(docx_bytes: bytes) -> str:
    """Extract paragraph text from a DOCX blob via python-docx."""
    try:
        from docx import Document  # python-docx
    except ImportError:
        logger.error("python-docx not installed; cannot extract MCR transcription DOCX")
        raise
    doc = Document(BytesIO(docx_bytes))
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _set_failed(session_factory, audio_file_id: str, reason: str) -> None:
    """Best-effort DB update on a failure path."""
    try:
        db = session_factory()
        try:
            row = db.query(UserAudioFile).filter(UserAudioFile.id == audio_file_id).one_or_none()
            if row is not None:
                row.transcription_status = "mcr_import_failed"
                row.last_activity_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Could not persist mcr_import_failed for %s (%s)", audio_file_id, reason)


def _import_meeting_audio(
    *,
    session_factory,
    s3_internal_cfg: S3Config,
    client: MCRClient,
    access_token: str,
    row: UserAudioFile,
    mcr_meeting_id: str,
) -> bool:
    """Download audio webm from MCR, upload to S3, mark row pending.

    Returns True if audio was imported, False if no audio was available.
    """
    try:
        resp = client.download_audio(access_token, mcr_meeting_id)
    except MCRApplicativeError as exc:
        # Tous les "applicatif" sur l'audio = audio indisponible côté MCR :
        #   - 404/410 : pas d'audio pour cette réunion
        #   - 403 + "feature flag" : audio download globalement désactivé en prod
        # Dans tous ces cas on retourne False pour que le caller fallback sur
        # le transcript DOCX (qui n'a pas le feature flag).
        logger.info(
            "mcr_importer: audio indisponible côté MCR pour meeting %s (%s) — fallback transcript",
            mcr_meeting_id, exc,
        )
        return False
    try:
        chunks = []
        total_bytes = 0
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                chunks.append(chunk)
                total_bytes += len(chunk)
    finally:
        resp.close()
    blob = b"".join(chunks)
    # Transcoder webm → m4a (AAC) avant l'upload S3. Kevent refuse .webm
    # (whitelist : .ogg .flac .mp3 .mp4 .wav .m4a). On utilise ffmpeg en
    # subprocess (déjà dans l'image internal-ingester).
    m4a_blob = _transcode_webm_to_m4a(blob, mcr_meeting_id)
    total_bytes = len(m4a_blob)
    stored_filename = f"MCRIMP_{uuid.uuid4().hex[:8]}_mcr-meeting-{mcr_meeting_id}.m4a"
    upload_fileobj(s3_internal_cfg, stored_filename, BytesIO(m4a_blob), content_type="audio/mp4")
    db = session_factory()
    try:
        db_row = db.query(UserAudioFile).filter(UserAudioFile.id == row.id).one()
        db_row.stored_filename = stored_filename
        db_row.file_size_bytes = total_bytes
        db_row.original_filename = stored_filename
        # Status 'pending' lets the existing transcription pipeline take over.
        # The pipeline-watchdog will detect this fresh row in its next tick.
        db_row.transcription_status = "pending"
        db_row.last_activity_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    return True


def _transcode_webm_to_m4a(webm_bytes: bytes, label: str) -> bytes:
    """Transcode webm bytes → m4a (AAC LC, mono, 16kHz) via ffmpeg subprocess.

    Optimisé pour Whisper : mono 16kHz suffit (Whisper resample de toute façon),
    AAC LC car c'est le codec audio standard MP4. -y pour overwrite, -loglevel
    error pour silence stdout sauf en cas d'erreur.

    Raises RuntimeError si ffmpeg foire (mais on garde le subprocess exit
    code dans le message pour diagnostic).
    """
    import subprocess, tempfile
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as fin, \
         tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as fout:
        fin.write(webm_bytes)
        fin.flush()
        in_path = fin.name
        out_path = fout.name
    try:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", in_path,
            "-vn",                    # pas de stream vidéo
            "-ac", "1",               # mono (Whisper)
            "-ar", "16000",           # 16kHz (Whisper)
            "-c:a", "aac",            # AAC LC
            "-b:a", "64k",            # bitrate raisonnable pour voix
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg transcode webm→m4a failed (rc={result.returncode}) "
                f"for {label}: {result.stderr.decode('utf-8', errors='replace')[:300]}"
            )
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(in_path)
            os.unlink(out_path)
        except Exception:
            pass


def _import_meeting_transcript(
    *,
    session_factory,
    client: MCRClient,
    access_token: str,
    row: UserAudioFile,
    mcr_meeting_id: str,
) -> bool:
    """Download DOCX transcription from MCR, persist text on the row.

    Returns True if a transcript was imported, False if MCR has no transcript.
    """
    try:
        docx_bytes = client.download_transcription_docx(access_token, mcr_meeting_id)
    except MCRApplicativeError as exc:
        if "No transcription available" in str(exc):
            return False
        raise
    text = _extract_docx_text(docx_bytes)
    db = session_factory()
    try:
        db_row = db.query(UserAudioFile).filter(UserAudioFile.id == row.id).one()
        db_row.transcription_text = text
        db_row.transcription_status = "mcr_transcript_only"
        db_row.transcription_engine = "mcr"
        db_row.transcription_completed_at = datetime.now(timezone.utc)
        db_row.last_activity_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()

    # Lance la chaîne LLM (glossary correction / cleaning / reformulation /
    # meeting analysis / key_points / absentee_summary / suggested_filename)
    # sur la transcription brute MCR — équivalent du pipeline kevent
    # post-Whisper, sans Whisper. Skip diarization / speaker_naming (pas
    # d'audio donc pas de speakers identifiables).
    _trigger_llm_chain_on_transcript(session_factory, str(row.id), text)
    return True


def _trigger_llm_chain_on_transcript(session_factory, audio_id: str, transcript: str) -> None:
    """Spawn un thread daemon qui exécute la chaîne LLM puis met à jour la row.

    Réutilise ``_run_llm_chain_for_audio`` et ``_set_user_audio_status`` de
    puller.py — même service donc import direct OK.
    """
    def _run():
        try:
            from app.puller import (
                _build_llm_client,
                _run_llm_chain_for_audio,
                _set_user_audio_status,
            )
        except Exception:
            logger.exception("mcr_importer: cannot import LLM chain helpers; transcript stays raw")
            return
        try:
            llm = _build_llm_client()
            if llm is None:
                logger.info("mcr_importer: LiteLLM non configuré, on garde le transcript brut")
                return
            chain_updates, chain_status = _run_llm_chain_for_audio(
                transcript,
                None,  # pas de speaker_tagged_text
                llm=llm,
                glossary_terms=None,  # fallback _GLOSSARY_TERMS statique
                include_metadata=True,
            )
            final_status = (
                "kevent_completed"
                if chain_status == "kevent_completed"
                else "kevent_partially_completed"
            )
            # transcription_engine reste "mcr" — le LLM enrichit mais l'origine
            # de la transcription brute c'est MCR.
            _set_user_audio_status(audio_id, final_status, **chain_updates)
            logger.info(
                "mcr_importer: LLM chain done audio=%s status=%s outputs=%d",
                audio_id, final_status, len(chain_updates),
            )
        except Exception:
            logger.exception("mcr_importer: LLM chain crashed for %s", audio_id)
            try:
                from app.puller import _set_user_audio_status
                _set_user_audio_status(audio_id, "kevent_partially_completed")
            except Exception:
                logger.exception("mcr_importer: also failed to mark partially_completed")

    threading.Thread(
        target=_run,
        name=f"mcr-transcript-llm-{audio_id[:8]}",
        daemon=True,
    ).start()


def _handle_message(session_factory, s3_internal_cfg: S3Config, message: dict) -> bool:
    """Callback wired into consume_queue.

    Returns True on success, False on non-retryable failure (the queue
    helper will not requeue False; True commits the ack).
    """
    audio_file_id = message.get("user_audio_file_id")
    mcr_meeting_id = str(message.get("mcr_meeting_id") or "")
    user_sub = message.get("user_sub") or ""
    user_email = message.get("user_email") or ""
    fallback_transcript = bool(message.get("fallback_transcript", True))

    if not (mcr_meeting_id and user_sub):
        logger.error("mcr_importer: malformed message %s", message)
        return False

    db = session_factory()
    try:
        # Si pas d'audio_file_id dans le message (cas standard depuis le refactor
        # 2026-05-23 — mesreunions-web n'a pas accès à postgres-internal), on
        # crée la row ici. Dédoublonnage via l'index partiel migration 019.
        row = None
        if audio_file_id:
            row = db.query(UserAudioFile).filter(UserAudioFile.id == audio_file_id).one_or_none()
        if row is None:
            # Cherche par (user_sub, mcr_meeting_id) — dédoublonnage standard
            row = (
                db.query(UserAudioFile)
                .filter(
                    UserAudioFile.user_sub == user_sub,
                    UserAudioFile.mcr_meeting_id == mcr_meeting_id,
                    UserAudioFile.origin == "mcr_import",
                )
                .one_or_none()
            )
        if row is None:
            # Nouvelle row à créer
            row = UserAudioFile(
                id=uuid.uuid4(),
                user_sub=user_sub,
                user_email=user_email,
                original_session_code="MCRIMP",
                original_filename=f"mcr-meeting-{mcr_meeting_id}.webm",
                stored_filename="",
                file_size_bytes=0,
                origin="mcr_import",
                transcription_status="mcr_import_pending",
                mcr_meeting_id=mcr_meeting_id,
                last_activity_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            logger.info("mcr_importer: created row id=%s for mcr_meeting_id=%s", row.id, mcr_meeting_id)
        else:
            # Existing row : on rebascule en pending pour relancer le traitement
            if row.transcription_status != "pending":
                row.transcription_status = "mcr_import_pending"
                row.last_activity_at = datetime.now(timezone.utc)
                db.commit()
        audio_file_id = row.id
    finally:
        db.close()

    try:
        access_token = _exchange_refresh_for_user(user_sub)
    except OIDCAuthError as exc:
        logger.warning("mcr_importer: auth failure for user_sub=%s (%s)", user_sub, exc)
        _set_failed(session_factory, audio_file_id, "auth")
        return False
    except OIDCTransientError as exc:
        logger.warning("mcr_importer: transient OIDC error (%s), will retry via queue", exc)
        raise  # let consume_queue retry
    except Exception:
        logger.exception("mcr_importer: unexpected OIDC error for user_sub=%s", user_sub)
        _set_failed(session_factory, audio_file_id, "oidc_other")
        return False

    client = MCRClient(
        gateway_url=_mcr_gateway(),
        oidc_token_endpoint=_oidc_token_endpoint(),
        oidc_client_id=_oidc_client_id(),
        oidc_client_secret=_oidc_client_secret(),
    )

    try:
        imported_audio = _import_meeting_audio(
            session_factory=session_factory,
            s3_internal_cfg=s3_internal_cfg,
            client=client,
            access_token=access_token,
            row=row,
            mcr_meeting_id=mcr_meeting_id,
        )
    except (MCRAuthError, OIDCAuthError) as exc:
        logger.warning("mcr_importer: MCR rejected token for %s (%s)", audio_file_id, exc)
        _set_failed(session_factory, audio_file_id, "mcr_auth")
        return False
    except MCRTransientError:
        logger.exception("mcr_importer: MCR transient on audio, will retry")
        raise
    except Exception:
        logger.exception("mcr_importer: unexpected error pulling audio for %s", audio_file_id)
        _set_failed(session_factory, audio_file_id, "audio")
        return False

    if imported_audio:
        return True

    if not fallback_transcript:
        _set_failed(session_factory, audio_file_id, "no_audio_no_fallback")
        return False

    try:
        imported_txt = _import_meeting_transcript(
            session_factory=session_factory,
            client=client,
            access_token=access_token,
            row=row,
            mcr_meeting_id=mcr_meeting_id,
        )
    except MCRTransientError:
        logger.exception("mcr_importer: MCR transient on transcript, will retry")
        raise
    except Exception:
        logger.exception("mcr_importer: unexpected error pulling transcript for %s", audio_file_id)
        _set_failed(session_factory, audio_file_id, "transcript")
        return False

    if not imported_txt:
        _set_failed(session_factory, audio_file_id, "no_transcript")
        return False

    return True


# ─── Worker entrypoint ────────────────────────────────────────────────

_started = False


def start_mcr_importer(session_factory, rabbit_cfg: Optional[RabbitMQConfig] = None) -> None:
    """Spawn the background consumer thread. Idempotent."""
    global _started
    if _started:
        return
    _started = True
    cfg = rabbit_cfg or RabbitMQConfig()
    s3_internal_cfg = load_s3_internal()

    def _run():
        logger.info("mcr_importer: starting consumer on %s", QUEUE_MCR_IMPORT)
        try:
            consume_queue(
                cfg,
                QUEUE_MCR_IMPORT,
                lambda m: _handle_message(session_factory, s3_internal_cfg, m),
                prefetch=1,
            )
        except Exception:
            logger.exception("mcr_importer: consumer loop crashed")

    threading.Thread(target=_run, daemon=True, name="mcr-importer").start()
