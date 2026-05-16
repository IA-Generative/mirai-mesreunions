"""Service sessions — helpers de domaine (lifecycle, storage, audio).

Tout ce qui ne dépend pas directement de Flask `request`/`jsonify` est ici.
Les routes (``routes.py``) appellent ces helpers et formattent la sortie HTTP.

Helpers transverses utilisés par d'autres modules (notamment ``devices``)
peuvent être importés via ``from app.modules.sessions.service import ...``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests as req

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
from libs.shared.app.config import INTERNAL_API_TOKEN  # noqa: E402
from libs.shared.app.models import UploadSession, UploadedFile, UploadStatus  # noqa: E402
from libs.shared.app.s3_helper import delete_object, object_exists  # noqa: E402

from app.runtime import (
    get_s3_upload_cfg, get_s3_processed_cfg, get_s3_internal_cfg,
    get_normalization_analysis_max_seconds,
)
from app.shared import request_internal_device_api

logger = logging.getLogger("mydevices_web.sessions.service")


# ── Constantes ──────────────────────────────────────────────────────────

TRASH_RETENTION_DAYS = int(os.getenv("TRASH_RETENTION_DAYS", "30"))
LOCAL_UPLOAD_SIMPLE_CODE_PREFIX = "L-"
LOCAL_UPLOAD_MAX_PER_SESSION = 9999
LOCAL_UPLOAD_DEVICE_LABEL = "Upload local"


TRANSCRIPT_KIND_TO_COLUMN = {
    "transcript": "transcription_text",
    "transcript-tagged": "speaker_tagged_text",
    "transcript-corrected": "glossary_corrected_text",
    "transcript-cleaned": "cleaned_text",
    "transcript-reformulated": "reformulated_text",
}


# ── Owned file resolution ───────────────────────────────────────────────


def get_owned_file(db, user_sub: str, file_id: str):
    """Return owned file row, ignoring trashed files and trashed sessions."""
    return (
        db.query(UploadedFile)
        .join(UploadSession, UploadSession.id == UploadedFile.session_id)
        .filter(
            UploadedFile.id == file_id,
            UploadSession.user_sub == user_sub,
            UploadedFile.trashed_at.is_(None),
            UploadSession.trashed_at.is_(None),
        )
        .first()
    )


# ── Storage resolution ──────────────────────────────────────────────────


def resolve_file_storage(file_obj: UploadedFile):
    if file_obj.transcoded_filename:
        return get_s3_processed_cfg(), file_obj.transcoded_filename
    return get_s3_upload_cfg(), file_obj.stored_filename


def resolve_source_storage(file_obj: UploadedFile):
    return get_s3_upload_cfg(), file_obj.stored_filename


def resolve_transcoded_storage(file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None, None
    return get_s3_processed_cfg(), file_obj.transcoded_filename


def resolve_transferred_storage(db, file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None, None
    session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
    if not session_obj:
        return None, None
    internal_key = f"{session_obj.user_sub}/{session_obj.simple_code}/{file_obj.transcoded_filename}"
    return get_s3_internal_cfg(), internal_key


def guess_audio_mime_from_key(key: str) -> str:
    ext = Path(key or "").suffix.lower()
    if ext in {".mp4", ".m4a"}:
        return "audio/mp4"
    if ext == ".wav":
        return "audio/wav"
    if ext == ".ogg":
        return "audio/ogg"
    if ext == ".opus":
        return "audio/ogg"
    if ext == ".mp3":
        return "audio/mpeg"
    return "application/octet-stream"


# ── Lifecycle ───────────────────────────────────────────────────────────


def compute_lifecycle_state(session, has_active_device: bool = False) -> str:
    now = datetime.now(timezone.utc)
    expires_at = session.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    qr_grace_past = bool(expires_at and expires_at < now)
    has_activity = (session.upload_count or 0) > 0
    if has_active_device:
        return "enrolled"
    if not qr_grace_past and not has_activity:
        return "pending_enrollment"
    if has_activity:
        return "expired_consumed"
    return "expired_unused"


# ── Trash purge ─────────────────────────────────────────────────────────


def purge_expired_trash(db, user_sub: str) -> tuple[int, int, int]:
    """Hard-delete des entries en corbeille > TRASH_RETENTION_DAYS."""
    threshold = datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)
    sessions_purged = 0
    files_purged = 0
    objects_deleted = 0
    preparations_purged = 0
    meetings_purged = 0
    s3_upload_cfg = get_s3_upload_cfg()
    s3_processed_cfg = get_s3_processed_cfg()
    try:
        result = request_internal_device_api(
            "POST", "/api/v1/preparations/purge",
            json_body={"user_sub": user_sub, "older_than_days": TRASH_RETENTION_DAYS},
        )
        preparations_purged = int(result.get("purged") or 0)
    except Exception:
        logger.debug("trash purge: preparation purge relay failed for user=%s",
                     user_sub, exc_info=True)
    try:
        result = request_internal_device_api(
            "POST", "/api/v1/meetings/purge",
            json_body={"user_sub": user_sub, "older_than_days": TRASH_RETENTION_DAYS},
        )
        meetings_purged = int(result.get("purged") or 0)
    except Exception:
        logger.debug("trash purge: meeting purge relay failed for user=%s",
                     user_sub, exc_info=True)

    trashed_files = (
        db.query(UploadedFile)
        .join(UploadSession, UploadSession.id == UploadedFile.session_id)
        .filter(
            UploadSession.user_sub == user_sub,
            UploadedFile.trashed_at.isnot(None),
            UploadedFile.trashed_at < threshold,
        )
        .all()
    )
    for f in trashed_files:
        for cfg, key in (
            (s3_upload_cfg, f.stored_filename),
            (s3_processed_cfg, f.transcoded_filename),
        ):
            if not key:
                continue
            try:
                delete_object(cfg, key)
                objects_deleted += 1
            except Exception:
                logger.debug("trash purge: S3 delete failed for %s", key, exc_info=True)
        try:
            t_cfg, t_key = resolve_transferred_storage(db, f)
            if t_cfg and t_key:
                delete_object(t_cfg, t_key)
                objects_deleted += 1
        except Exception:
            logger.debug("trash purge: transferred S3 delete failed for %s", f.id, exc_info=True)
        db.delete(f)
        files_purged += 1

    trashed_sessions = (
        db.query(UploadSession)
        .filter(
            UploadSession.user_sub == user_sub,
            UploadSession.trashed_at.isnot(None),
            UploadSession.trashed_at < threshold,
        )
        .all()
    )
    for s in trashed_sessions:
        for f in s.uploads:
            for cfg, key in (
                (s3_upload_cfg, f.stored_filename),
                (s3_processed_cfg, f.transcoded_filename),
            ):
                if not key:
                    continue
                try:
                    delete_object(cfg, key)
                    objects_deleted += 1
                except Exception:
                    logger.debug("trash purge (session): S3 delete failed for %s", key, exc_info=True)
            files_purged += 1
        db.delete(s)
        sessions_purged += 1

    if sessions_purged or files_purged:
        db.commit()
    if sessions_purged or files_purged or preparations_purged or meetings_purged:
        logger.info(
            "trash purge: user=%s sessions=%s files=%s s3_objects=%s preparations=%s meetings=%s",
            user_sub, sessions_purged, files_purged, objects_deleted,
            preparations_purged, meetings_purged,
        )
    return sessions_purged, files_purged, objects_deleted


# ── Audio outputs lookup ────────────────────────────────────────────────


def _request_internal_ingester_api(path: str, *, json_body=None, params=None,
                                    method: str = "POST", timeout: int = 10):
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL", "http://internal-ingester:8090").rstrip("/")
    resp = req.request(
        method,
        f"{base}{path}",
        json=json_body,
        params=params,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise req.HTTPError(
            f"internal-ingester {path} → {resp.status_code}: {resp.text[:200]}",
            response=resp,
        )
    return resp.json()


def lookup_audio_outputs(db, file_obj: UploadedFile):
    if not file_obj.transcoded_filename:
        return None
    session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
    if not session_obj:
        return None
    try:
        return _request_internal_ingester_api(
            "/api/v1/audio/lookup",
            json_body={
                "user_sub": session_obj.user_sub,
                "simple_code": session_obj.simple_code,
                "stored_filename": file_obj.transcoded_filename,
            },
        )
    except req.RequestException:
        logger.exception("internal-ingester lookup failed for file_id=%s", file_obj.id)
        return None


# ── Loudnorm ────────────────────────────────────────────────────────────


def run_loudnorm_measure(input_path: str, target_i: float = -16.0,
                          target_tp: float = -1.5, target_lra: float = 11.0):
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", input_path,
        "-t", str(get_normalization_analysis_max_seconds()),
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


# ── Download filename helpers ──────────────────────────────────────────


def maybe_prepend_key_points(body: str, key_points_md: str | None, fmt: str) -> str:
    if not key_points_md or fmt not in ("md", "docx", "odt"):
        return body
    return f"## Points clés\n\n{key_points_md}\n\n---\n\n{body}"


def build_download_basename(file_obj: UploadedFile, audio: dict | None, slot: str) -> str:
    stem = ""
    if audio and audio.get("suggested_filename"):
        stem = audio["suggested_filename"].strip()
    if not stem:
        stem = Path(file_obj.original_filename or "audio").stem
        if slot:
            stem = f"{stem}_{slot}"
    date_src = None
    if audio and audio.get("transcription_completed_at"):
        try:
            date_src = audio["transcription_completed_at"][:10]
        except Exception:
            date_src = None
    if not date_src and file_obj.created_at:
        date_src = file_obj.created_at.strftime("%Y-%m-%d")
    if date_src and date_src not in stem:
        stem = f"{stem} {date_src}"
    return stem
