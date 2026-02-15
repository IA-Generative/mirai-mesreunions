"""
Admin Portal Service
====================
OIDC-protected admin dashboard for monitoring upload/transcode/transfer/transcription.
"""

import logging
import mimetypes
import os
import sys
import json
import base64
import re
import subprocess
import tempfile
import threading
import time
import socket
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode
import secrets
import requests as req
import boto3
from botocore.config import Config as BotoConfig

from authlib.integrations.flask_client import OAuth
from flask import Flask, abort, jsonify, redirect, render_template_string, request, send_file, session, url_for

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    OIDCConfig,
    SECRET_KEY,
    load_ext_db,
    load_int_db,
    load_s3_upload,
    load_s3_processed,
    load_s3_internal,
)
from libs.shared.app.database import create_session_factory
from libs.shared.app.models import (
    UploadSession, UploadedFile, UserAudioFile, TranscriptionEvent, UploadStatus, DeviceEnrollment,
)
from libs.shared.app.s3_helper import delete_object, download_fileobj, get_s3_client

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

NORMALIZATION_CACHE_TTL_SECONDS = max(60, int(os.getenv("NORMALIZATION_CACHE_TTL_SECONDS", "3600")))
NORMALIZATION_MAX_COMPUTE_PER_REFRESH = max(0, int(os.getenv("NORMALIZATION_MAX_COMPUTE_PER_REFRESH", "0")))
ADMIN_S3_LIST_CONNECT_TIMEOUT_SECONDS = max(1, int(os.getenv("ADMIN_S3_LIST_CONNECT_TIMEOUT_SECONDS", "2")))
ADMIN_S3_LIST_READ_TIMEOUT_SECONDS = max(1, int(os.getenv("ADMIN_S3_LIST_READ_TIMEOUT_SECONDS", "3")))


def _parse_allowed_users() -> set[str]:
    raw = os.getenv("ADMIN_ALLOWED_USERS", "admin")
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


def _as_iso(dt):
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024


def _decode_jwt_payload_unverified(token_value: str) -> dict:
    """Best-effort JWT payload decode (no signature verification)."""
    try:
        parts = token_value.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = SECRET_KEY

    oidc_cfg = OIDCConfig()
    allowed_users = _parse_allowed_users()

    ext_db_cfg = load_ext_db()
    int_db_cfg = load_int_db()
    s3_upload_cfg = load_s3_upload()
    s3_processed_cfg = load_s3_processed()
    s3_internal_cfg = load_s3_internal()
    s3_map = {
        "upload": s3_upload_cfg,
        "processed": s3_processed_cfg,
        "internal": s3_internal_cfg,
    }

    ExtSessionLocal = create_session_factory(ext_db_cfg)
    IntSessionLocal = create_session_factory(int_db_cfg)

    oidc_public_issuer = oidc_cfg.issuer.rstrip("/")
    oidc_internal_issuer = os.getenv("OIDC_INTERNAL_ISSUER", "http://keycloak:8080/realms/audio-upload").rstrip("/")

    # Keep OAuth object for compatibility, but use explicit OIDC flow below for reliability in Docker networking.
    oauth = OAuth(app)
    base_path_raw = (os.getenv("ADMIN_BASE_PATH", "") or "").strip()
    if base_path_raw and not base_path_raw.startswith("/"):
        base_path_raw = f"/{base_path_raw}"
    base_path = base_path_raw.rstrip("/")

    def _with_base(path: str) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base_path}{path}" if base_path else path

    def current_user():
        return session.get("user")

    def is_admin(user: dict) -> bool:
        if not allowed_users:
            return True
        candidates = {
            str(user.get("preferred_username", "")).lower(),
            str(user.get("email", "")).lower(),
            str(user.get("name", "")).lower(),
            str(user.get("sub", "")).lower(),
        }
        return any(c in allowed_users for c in candidates)

    def require_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(_with_base(url_for("login")))
            if not is_admin(user):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "forbidden"}), 403
                return "Acces refuse: compte non autorise pour l'administration.", 403
            return view(*args, **kwargs)

        return wrapped

    def bucket_overview(name: str, cfg, sample_limit: int = 20):
        try:
            # Keep dashboard responsive even when one bucket is slow/unreachable.
            client = boto3.client(
                "s3",
                endpoint_url=cfg.endpoint,
                aws_access_key_id=cfg.access_key,
                aws_secret_access_key=cfg.secret_key,
                region_name=cfg.region,
                config=BotoConfig(
                    signature_version="s3v4",
                    connect_timeout=ADMIN_S3_LIST_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=ADMIN_S3_LIST_READ_TIMEOUT_SECONDS,
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
            response = client.list_objects_v2(Bucket=cfg.bucket, MaxKeys=sample_limit)
            items = response.get("Contents", [])
            object_count = response.get("KeyCount", len(items))
            total_size = sum(int(obj.get("Size", 0)) for obj in items)
            return {
                "name": name,
                "bucket": cfg.bucket,
                "endpoint": cfg.endpoint,
                "status": "ok",
                "sample_count": object_count,
                "sample_size_bytes": total_size,
                "sample_size_human": _format_size(total_size),
                "is_truncated": bool(response.get("IsTruncated", False)),
                "objects": [
                    {
                        "key": obj.get("Key", ""),
                        "size_bytes": int(obj.get("Size", 0)),
                        "size_human": _format_size(int(obj.get("Size", 0))),
                        "last_modified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
                    }
                    for obj in items
                ],
            }
        except Exception as exc:
            logger.warning("Unable to list bucket %s (%s): %s", name, cfg.bucket, exc)
            return {
                "name": name,
                "bucket": cfg.bucket,
                "endpoint": cfg.endpoint,
                "status": "error",
                "error": str(exc),
                "sample_count": 0,
                "sample_size_bytes": 0,
                "sample_size_human": "0 B",
                "is_truncated": False,
                "objects": [],
            }

    def dashboard_data(limit_sessions: int = 30):
        normalization_cache: dict[str, dict] = getattr(app, "_normalization_cache", {})
        normalization_cache_lock: threading.Lock = getattr(app, "_normalization_cache_lock", threading.Lock())
        app._normalization_cache = normalization_cache
        app._normalization_cache_lock = normalization_cache_lock

        def run_loudnorm_measure(path: str, target_i: float = -16.0, target_tp: float = -1.5, target_lra: float = 11.0):
            cmd = [
                "ffmpeg", "-hide_banner", "-nostats", "-i", path,
                "-af", f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
                "-f", "null", "-",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg loudnorm analyze failed")
            matches = re.findall(r"\{[\s\S]*?\}", proc.stderr or "")
            if not matches:
                raise RuntimeError("loudnorm json not found")
            data = json.loads(matches[-1])
            return {
                "i": float(data.get("input_i")),
                "tp": float(data.get("input_tp")),
                "lra": float(data.get("input_lra")),
            }

        def compute_normalization_impact(file_obj: UploadedFile):
            with tempfile.TemporaryDirectory() as tmpdir:
                src_suffix = Path(file_obj.stored_filename or "").suffix or ".audio"
                out_suffix = Path(file_obj.transcoded_filename or "").suffix or ".wav"
                src_path = os.path.join(tmpdir, f"src{src_suffix}")
                out_path = os.path.join(tmpdir, f"out{out_suffix}")

                with open(src_path, "wb") as srcf:
                    srcf.write(download_fileobj(s3_upload_cfg, file_obj.stored_filename).read())
                with open(out_path, "wb") as outf:
                    outf.write(download_fileobj(s3_processed_cfg, file_obj.transcoded_filename).read())

                source = run_loudnorm_measure(src_path)
                normalized = run_loudnorm_measure(out_path)
                target_i = -16.0
                source_dist = abs(source["i"] - target_i)
                normalized_dist = abs(normalized["i"] - target_i)
                return {
                    "source": source,
                    "normalized": normalized,
                    "delta": {
                        "i": round(normalized["i"] - source["i"], 2),
                        "tp": round(normalized["tp"] - source["tp"], 2),
                        "lra": round(normalized["lra"] - source["lra"], 2),
                    },
                    "improvement_to_target_lufs": round(source_dist - normalized_dist, 2),
                }

        def get_normalization_impact(file_obj: UploadedFile, allow_compute: bool):
            if not file_obj.transcoded_filename:
                return None, "not_transcoded"

            now = time.time()
            cache_key = str(file_obj.id)
            with normalization_cache_lock:
                hit = normalization_cache.get(cache_key)
                if hit and now - float(hit.get("ts", 0)) < NORMALIZATION_CACHE_TTL_SECONDS:
                    return hit.get("value"), hit.get("error")

            if not allow_compute:
                return None, "pending_compute"

            value = None
            error = None
            try:
                value = compute_normalization_impact(file_obj)
            except Exception as exc:
                logger.warning("Normalization impact failed for %s: %s", file_obj.id, exc)
                error = "compute_failed"

            with normalization_cache_lock:
                normalization_cache[cache_key] = {"ts": now, "value": value, "error": error}
                if len(normalization_cache) > 500:
                    oldest = sorted(normalization_cache.items(), key=lambda kv: kv[1].get("ts", 0))[:100]
                    for key, _ in oldest:
                        normalization_cache.pop(key, None)
            return value, error

        ext_db = ExtSessionLocal()
        int_db = IntSessionLocal()
        try:
            sessions = (
                ext_db.query(UploadSession)
                .order_by(UploadSession.created_at.desc())
                .limit(limit_sessions)
                .all()
            )
            session_ids = [s.id for s in sessions]
            simple_codes = [s.simple_code for s in sessions]

            files = []
            if session_ids:
                files = (
                    ext_db.query(UploadedFile)
                    .filter(UploadedFile.session_id.in_(session_ids))
                    .order_by(UploadedFile.created_at.desc())
                    .all()
                )

            int_db_available = True
            try:
                with socket.create_connection((int_db_cfg.host, int_db_cfg.port), timeout=2):
                    pass
            except OSError:
                int_db_available = False
                logger.warning(
                    "Internal DB unreachable from admin-portal: host=%s port=%s",
                    int_db_cfg.host,
                    int_db_cfg.port,
                )

            internal_files = []
            if int_db_available and simple_codes:
                internal_files = (
                    int_db.query(UserAudioFile)
                    .filter(UserAudioFile.original_session_code.in_(simple_codes))
                    .order_by(UserAudioFile.created_at.desc())
                    .all()
                )
            devices = []
            if int_db_available:
                devices = (
                    int_db.query(DeviceEnrollment)
                    .order_by(DeviceEnrollment.created_at.desc())
                    .limit(200)
                    .all()
                )

            internal_index = {}
            for it in internal_files:
                key = (it.original_session_code, it.original_filename)
                if key not in internal_index:
                    internal_index[key] = it

            by_session = {str(s.id): [] for s in sessions}
            compute_budget = NORMALIZATION_MAX_COMPUTE_PER_REFRESH
            for f in files:
                match = internal_index.get((f.session.simple_code, f.original_filename))
                allow_compute = compute_budget > 0
                norm_impact, norm_error = get_normalization_impact(f, allow_compute=allow_compute)
                if allow_compute and (norm_impact is not None or norm_error == "compute_failed"):
                    compute_budget -= 1
                by_session[str(f.session_id)].append(
                    {
                        "file_id": str(f.id),
                        "name": f.original_filename,
                        "status": f.status.value,
                        "status_message": f.status_message or "",
                        "created_at": _as_iso(f.created_at),
                        "updated_at": _as_iso(f.updated_at),
                        "transcription_status": match.transcription_status if match else "pending_or_not_transferred",
                        "transcription_started_at": _as_iso(match.transcription_started_at) if match else None,
                        "transcription_completed_at": _as_iso(match.transcription_completed_at) if match else None,
                        "transcription_preview": (match.transcription_text[:120] + "...")
                        if (match and match.transcription_text and len(match.transcription_text) > 120)
                        else (match.transcription_text if match else None),
                        "normalization_impact": norm_impact,
                        "normalization_error": norm_error,
                        "internal_bucket": "internal" if match else None,
                        "internal_key": match.stored_filename if match else None,
                    }
                )

            sessions_payload = []
            for s in sessions:
                sessions_payload.append(
                    {
                        "session_id": str(s.id),
                        "simple_code": s.simple_code,
                        "user_email": s.user_email,
                        "status": s.status.value,
                        "upload_count": s.upload_count,
                        "max_uploads": s.max_uploads,
                        "created_at": _as_iso(s.created_at),
                        "expires_at": _as_iso(s.expires_at),
                        "files": by_session.get(str(s.id), []),
                    }
                )

            summary = {
                "sessions": len(sessions_payload),
                "files": len(files),
                "quarantined": sum(1 for f in files if f.status == UploadStatus.QUARANTINED),
                "transcription_completed": sum(1 for uf in internal_files if uf.transcription_status == "completed"),
                "transcription_processing": sum(1 for uf in internal_files if uf.transcription_status == "processing"),
                "transcription_failed": sum(1 for uf in internal_files if uf.transcription_status == "failed"),
                "devices_total": len(devices),
                "devices_active": sum(1 for d in devices if d.status == "active"),
                "devices_revoked": sum(1 for d in devices if d.status == "revoked"),
            }

            s3_buckets = [
                bucket_overview("upload", s3_upload_cfg),
                bucket_overview("processed", s3_processed_cfg),
                bucket_overview("internal", s3_internal_cfg),
            ]
            s3_summary = {
                "bucket_count": len(s3_buckets),
                "healthy_buckets": sum(1 for b in s3_buckets if b["status"] == "ok"),
                "sample_objects": sum(int(b["sample_count"]) for b in s3_buckets),
                "sample_total_size_bytes": sum(int(b["sample_size_bytes"]) for b in s3_buckets),
            }
            s3_summary["sample_total_size_human"] = _format_size(s3_summary["sample_total_size_bytes"])

            events = []
            if int_db_available:
                events_query = int_db.query(TranscriptionEvent).order_by(TranscriptionEvent.created_at.desc())
                if simple_codes:
                    events_query = events_query.filter(TranscriptionEvent.original_session_code.in_(simple_codes))
                events = events_query.limit(100).all()
            events_payload = []
            for e in events:
                meta = {}
                if e.metadata_json:
                    try:
                        meta = json.loads(e.metadata_json)
                    except Exception:
                        meta = {"raw": e.metadata_json}
                events_payload.append(
                    {
                        "id": str(e.id),
                        "audio_file_id": str(e.audio_file_id),
                        "session_code": e.original_session_code,
                        "event_type": e.event_type,
                        "message": e.message or "",
                        "metadata": meta,
                        "created_at": _as_iso(e.created_at),
                    }
                )

            return {
                "summary": summary,
                "sessions": sessions_payload,
                "devices": [
                    {
                        "device_id": str(d.id),
                        "user_sub": d.user_sub,
                        "device_name": d.device_name or "",
                        "status": d.status,
                        "simple_code": d.simple_code,
                        "last_seen_at": _as_iso(d.last_seen_at),
                        "retention_expires_at": _as_iso(d.retention_expires_at),
                        "created_at": _as_iso(d.created_at),
                    }
                    for d in devices
                ],
                "s3": {
                    "summary": s3_summary,
                    "buckets": s3_buckets,
                },
                "transcription_events": events_payload,
            }
        finally:
            ext_db.close()
            int_db.close()

    def purge_one_file(ext_db, int_db, file_obj: UploadedFile) -> dict:
        result = {
            "deleted_external_db": 0,
            "deleted_internal_db": 0,
            "deleted_events": 0,
            "deleted_s3_objects": 0,
            "s3_delete_errors": 0,
        }
        session_obj = file_obj.session

        if file_obj.stored_filename:
            try:
                delete_object(s3_upload_cfg, file_obj.stored_filename)
                result["deleted_s3_objects"] += 1
            except Exception:
                result["s3_delete_errors"] += 1
                logger.warning("Failed deleting upload object: %s", file_obj.stored_filename)

        if file_obj.transcoded_filename:
            try:
                delete_object(s3_processed_cfg, file_obj.transcoded_filename)
                result["deleted_s3_objects"] += 1
            except Exception:
                result["s3_delete_errors"] += 1
                logger.warning("Failed deleting processed object: %s", file_obj.transcoded_filename)

        internal_records = []
        if session_obj and session_obj.user_sub and session_obj.simple_code and file_obj.transcoded_filename:
            expected_key = f"{session_obj.user_sub}/{session_obj.simple_code}/{file_obj.transcoded_filename}"
            internal_records = (
                int_db.query(UserAudioFile)
                .filter(UserAudioFile.stored_filename == expected_key)
                .all()
            )

        for rec in internal_records:
            try:
                if rec.stored_filename:
                    delete_object(s3_internal_cfg, rec.stored_filename)
                    result["deleted_s3_objects"] += 1
            except Exception:
                result["s3_delete_errors"] += 1
                logger.warning("Failed deleting internal object: %s", rec.stored_filename)

            result["deleted_events"] += (
                int_db.query(TranscriptionEvent)
                .filter(TranscriptionEvent.audio_file_id == rec.id)
                .delete(synchronize_session=False)
            )
            int_db.delete(rec)
            result["deleted_internal_db"] += 1

        if session_obj and session_obj.upload_count and session_obj.upload_count > 0:
            session_obj.upload_count -= 1

        ext_db.delete(file_obj)
        result["deleted_external_db"] = 1
        return result

    @app.route("/")
    @require_auth
    def index():
        return render_template_string(INDEX_TEMPLATE, user=current_user())

    @app.route("/login")
    def login():
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        session["oidc_state"] = state
        session["oidc_nonce"] = nonce
        params = {
            "response_type": "code",
            "client_id": oidc_cfg.client_id,
            "redirect_uri": oidc_cfg.redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
        }
        auth_url = f"{oidc_public_issuer}/protocol/openid-connect/auth?{urlencode(params)}"
        return redirect(auth_url)

    @app.route("/auth/callback")
    def auth_callback():
        # Callback may be replayed by browser/reload. Avoid second token exchange.
        if session.get("user"):
            session.pop("oidc_state", None)
            session.pop("oidc_nonce", None)
            return redirect(url_for("index"))

        state = request.args.get("state", "")
        code = request.args.get("code", "")
        if not code or not state or state != session.get("oidc_state"):
            return "OIDC callback invalide (state/code).", 400
        try:
            token_resp = req.post(
                f"{oidc_internal_issuer}/protocol/openid-connect/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": oidc_cfg.redirect_uri,
                    "client_id": oidc_cfg.client_id,
                    "client_secret": oidc_cfg.client_secret,
                },
                timeout=10,
            )
        except req.RequestException:
            logger.exception("OIDC token endpoint unreachable")
            return "OIDC indisponible (token endpoint). Réessaie.", 502

        if token_resp.status_code >= 400:
            body = (token_resp.text or "")[:500]
            logger.warning(
                "OIDC token exchange failed: status=%s body=%s",
                token_resp.status_code,
                body,
            )
            if "invalid_grant" in body or "Code not valid" in body:
                session.pop("oidc_state", None)
                session.pop("oidc_nonce", None)
                return redirect(url_for("login"))
            return "Echec de connexion OIDC (code expiré ou déjà utilisé).", 400

        try:
            token = token_resp.json()
        except Exception:
            logger.warning("OIDC token response is not JSON: %s", (token_resp.text or "")[:300])
            return "Réponse OIDC invalide (token).", 502
        try:
            userinfo_resp = req.get(
                f"{oidc_internal_issuer}/protocol/openid-connect/userinfo",
                headers={"Authorization": f"Bearer {token.get('access_token', '')}"},
                timeout=10,
            )
            if userinfo_resp.status_code >= 400:
                logger.warning(
                    "OIDC userinfo failed: status=%s body=%s",
                    userinfo_resp.status_code,
                    (userinfo_resp.text or "")[:500],
                )
                userinfo = _decode_jwt_payload_unverified(token.get("id_token", ""))
                if not userinfo:
                    return "Echec de récupération du profil OIDC.", 400
                logger.info("OIDC userinfo fallback to id_token claims")
            else:
                userinfo = userinfo_resp.json()
        except Exception:
            logger.exception("Failed to fetch userinfo from Keycloak")
            userinfo = _decode_jwt_payload_unverified(token.get("id_token", ""))
            if not userinfo:
                return "Erreur OIDC (userinfo). Réessaie.", 502
            logger.info("OIDC userinfo exception fallback to id_token claims")
        session["user"] = {
            "sub": userinfo.get("sub", ""),
            "email": userinfo.get("email", ""),
            "name": userinfo.get("name", userinfo.get("preferred_username", "")),
            "preferred_username": userinfo.get("preferred_username", ""),
        }
        session["id_token"] = token.get("id_token", "")
        session.pop("oidc_state", None)
        session.pop("oidc_nonce", None)
        return redirect(_with_base(url_for("index")))

    @app.route("/logout")
    def logout():
        id_token_hint = session.get("id_token")
        session.clear()

        post_logout_redirect_uri = oidc_cfg.redirect_uri.replace("/auth/callback", "/")
        params = {
            "post_logout_redirect_uri": post_logout_redirect_uri,
            "client_id": oidc_cfg.client_id,
        }
        if id_token_hint:
            params["id_token_hint"] = id_token_hint
        logout_url = f"{oidc_public_issuer}/protocol/openid-connect/logout?{urlencode(params)}"
        return redirect(logout_url)

    @app.route("/api/dashboard")
    @require_auth
    def api_dashboard():
        try:
            return jsonify(dashboard_data())
        except Exception:
            logger.exception("Admin dashboard aggregation failed")
            return jsonify(
                {
                    "summary": {
                        "sessions": 0,
                        "files": 0,
                        "transcription_completed": 0,
                        "transcription_processing": 0,
                        "transcription_failed": 0,
                        "devices_total": 0,
                        "devices_active": 0,
                        "devices_revoked": 0,
                    },
                    "sessions": [],
                    "devices": [],
                    "s3": {
                        "summary": {
                            "bucket_count": 0,
                            "healthy_buckets": 0,
                            "sample_objects": 0,
                            "sample_total_size_bytes": 0,
                            "sample_total_size_human": "0 B",
                        },
                        "buckets": [],
                    },
                    "transcription_events": [],
                    "error": "dashboard_unavailable",
                }
            ), 200

    @app.route("/api/purge-file", methods=["POST"])
    @require_auth
    def api_purge_file():
        payload = request.get_json(silent=True) or {}
        file_id = (payload.get("file_id") or "").strip()
        if not file_id:
            return jsonify({"error": "file_id requis"}), 400

        ext_db = ExtSessionLocal()
        int_db = IntSessionLocal()
        try:
            file_obj = ext_db.query(UploadedFile).filter(UploadedFile.id == file_id).first()
            if not file_obj:
                return jsonify({"error": "fichier introuvable"}), 404

            stats = purge_one_file(ext_db, int_db, file_obj)
            int_db.commit()
            ext_db.commit()
            return jsonify({"ok": True, **stats})
        except Exception:
            int_db.rollback()
            ext_db.rollback()
            logger.exception("Admin purge-file failed for %s", file_id)
            return jsonify({"error": "purge_failed"}), 500
        finally:
            ext_db.close()
            int_db.close()

    @app.route("/api/purge-all-files", methods=["POST"])
    @require_auth
    def api_purge_all_files():
        ext_db = ExtSessionLocal()
        int_db = IntSessionLocal()
        agg = {
            "deleted_external_db": 0,
            "deleted_internal_db": 0,
            "deleted_events": 0,
            "deleted_s3_objects": 0,
            "s3_delete_errors": 0,
            "failed_files": 0,
        }
        try:
            files = (
                ext_db.query(UploadedFile)
                .order_by(UploadedFile.created_at.desc())
                .all()
            )
            for f in files:
                try:
                    stats = purge_one_file(ext_db, int_db, f)
                    int_db.commit()
                    ext_db.commit()
                    for k in ("deleted_external_db", "deleted_internal_db", "deleted_events", "deleted_s3_objects", "s3_delete_errors"):
                        agg[k] += int(stats[k])
                except Exception:
                    int_db.rollback()
                    ext_db.rollback()
                    agg["failed_files"] += 1
                    logger.exception("Admin purge-all failed for file=%s", f.id)

            return jsonify({"ok": True, **agg})
        finally:
            ext_db.close()
            int_db.close()

    @app.route("/api/devices/revoke-one", methods=["POST"])
    @require_auth
    def api_revoke_one_device():
        payload = request.get_json(silent=True) or {}
        device_id = (payload.get("device_id") or "").strip()
        if not device_id:
            return jsonify({"error": "device_id requis"}), 400
        int_db = IntSessionLocal()
        try:
            rec = int_db.query(DeviceEnrollment).filter(DeviceEnrollment.id == device_id).first()
            if not rec:
                return jsonify({"error": "not_found"}), 404
            rec.status = "revoked"
            rec.revoked_reason = "revoked_by_admin_portal"
            rec.revoked_at = datetime.now(timezone.utc)
            rec.updated_at = rec.revoked_at
            int_db.commit()
            return jsonify({"ok": True})
        except Exception:
            int_db.rollback()
            logger.exception("Admin revoke device failed for %s", device_id)
            return jsonify({"error": "device_revoke_failed"}), 500
        finally:
            int_db.close()

    @app.route("/api/devices/revoke-all", methods=["POST"])
    @require_auth
    def api_revoke_all_devices():
        int_db = IntSessionLocal()
        try:
            now = datetime.now(timezone.utc)
            updated = (
                int_db.query(DeviceEnrollment)
                .filter(DeviceEnrollment.status == "active")
                .update(
                    {
                        DeviceEnrollment.status: "revoked",
                        DeviceEnrollment.revoked_reason: "revoked_all_by_admin_portal",
                        DeviceEnrollment.revoked_at: now,
                        DeviceEnrollment.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            int_db.commit()
            return jsonify({"ok": True, "revoked": int(updated)})
        except Exception:
            int_db.rollback()
            logger.exception("Admin revoke all devices failed")
            return jsonify({"error": "device_revoke_all_failed"}), 500
        finally:
            int_db.close()

    @app.route("/api/s3/download")
    @require_auth
    def api_s3_download():
        bucket_name = (request.args.get("bucket") or "").strip()
        key = (request.args.get("key") or "").strip()
        if not bucket_name or not key:
            abort(400, "bucket and key are required")

        cfg = s3_map.get(bucket_name)
        if cfg is None:
            abort(400, "invalid bucket")

        try:
            data = download_fileobj(cfg, key)
        except Exception:
            logger.exception("Failed to download object from S3: bucket=%s key=%s", bucket_name, key)
            abort(404, "object not found")

        filename = os.path.basename(key) or "download.bin"
        mime_type, _ = mimetypes.guess_type(filename)
        return send_file(
            data,
            mimetype=mime_type or "application/octet-stream",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/s3/stream")
    @require_auth
    def api_s3_stream():
        bucket_name = (request.args.get("bucket") or "").strip()
        key = (request.args.get("key") or "").strip()
        if not bucket_name or not key:
            abort(400, "bucket and key are required")

        cfg = s3_map.get(bucket_name)
        if cfg is None:
            abort(400, "invalid bucket")

        try:
            data = download_fileobj(cfg, key)
        except Exception:
            logger.exception("Failed to stream object from S3: bucket=%s key=%s", bucket_name, key)
            abort(404, "object not found")

        filename = os.path.basename(key) or "audio.wav"
        mime_type, _ = mimetypes.guess_type(filename)
        return send_file(
            data,
            mimetype=mime_type or "audio/wav",
            as_attachment=False,
            download_name=filename,
        )

    return app


application = create_app()


INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang=\"fr\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>Admin Portal</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f6f7fb; color: #101827; }
    .wrap { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
    .top { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
    .card { background: #fff; border-radius: 12px; padding: 14px; box-shadow: 0 1px 8px rgba(0,0,0,.06); margin-bottom: 12px; }
    .summary { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; }
    .kpi { background: #fff; border-radius: 10px; padding: 10px; box-shadow: 0 1px 8px rgba(0,0,0,.05); }
    .kpi .n { font-weight: 800; font-size: 20px; }
    .session { border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px; margin-bottom: 10px; }
    .files { margin-top: 8px; }
    .file { padding: 8px; border-radius: 8px; background: #f9fafb; margin-bottom: 6px; font-size: 13px; }
    .file-name { display: block; overflow-wrap: anywhere; word-break: break-word; }
    .file-actions a { margin-right: 8px; font-size: 12px; }
    .file audio { width: 100%; margin-top: 6px; }
    .pill { display: inline-block; border-radius: 999px; background: #eef2ff; color: #1e3a8a; padding: 1px 8px; margin-right: 6px; font-size: 12px; }
    .muted { color: #667085; font-size: 12px; }
    .warn { color: #92400e; }
    .bucket { border: 1px solid #e5e7eb; border-radius: 10px; padding: 10px; margin-bottom: 8px; }
    .obj { padding: 6px; background: #f9fafb; border-radius: 8px; margin-top: 6px; font-size: 12px; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"top\">
      <div>
        <h2 style=\"margin:0\">Administration Pipeline Audio</h2>
        <div class=\"muted\">Connecte: {{ user.email or user.preferred_username or user.sub }}</div>
      </div>
      <div style=\"display:flex;align-items:center;gap:10px\">
        <button id=\"revoke-all-devices-btn\" style=\"font-size:12px;padding:4px 8px;border:1px solid #d0d5dd;border-radius:8px;background:#fff;cursor:pointer\">Révoquer tous les devices</button>
        <button id=\"purge-all-btn\" style=\"font-size:12px;padding:4px 8px;border:1px solid #d0d5dd;border-radius:8px;background:#fff;cursor:pointer\">Purger tous les fichiers</button>
        <a id=\"logout-link\" href=\"logout\">Se deconnecter</a>
      </div>
    </div>

    <div class=\"summary\" id=\"summary\"></div>
    <div class=\"card\">
      <div style=\"display:flex;justify-content:space-between;align-items:center\">
        <strong>Devices enrôlés</strong>
        <span class=\"muted\">Gestion des appareils navigateur</span>
      </div>
      <div id=\"devices\" style=\"margin-top:10px\"></div>
    </div>
    <div class=\"card\">
      <div style=\"display:flex;justify-content:space-between;align-items:center\">
        <strong>Buckets S3</strong>
        <span class=\"muted\">Apercu echantillon (20 objets max / bucket)</span>
      </div>
      <div id=\"s3-summary\" class=\"muted\" style=\"margin-top:8px\"></div>
      <div id=\"s3-buckets\" style=\"margin-top:8px\"></div>
    </div>
    <div class=\"card\">
      <div style=\"display:flex;justify-content:space-between;align-items:center\">
        <strong>Appels Stub Transcription</strong>
        <span class=\"muted\">100 derniers evenements</span>
      </div>
      <div id=\"stt-events\" style=\"margin-top:10px\"></div>
    </div>
    <div class=\"card\">
      <div style=\"display:flex;justify-content:space-between;align-items:center\">
        <strong>Sessions recentes</strong>
        <span class=\"muted\">Refresh auto 5s</span>
      </div>
      <div id=\"sessions\" style=\"margin-top:10px\"></div>
    </div>
  </div>

<script>
function esc(v) { return (v || '').toString().replace(/[&<>\"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[s])); }

function renderSummary(s) {
  const root = document.getElementById('summary');
  root.innerHTML = `
    <div class=\"kpi\"><div class=\"muted\">Sessions</div><div class=\"n\">${s.sessions}</div></div>
    <div class=\"kpi\"><div class=\"muted\">Fichiers</div><div class=\"n\">${s.files}</div></div>
    <div class=\"kpi\"><div class=\"muted\">En quarantaine</div><div class=\"n\">${s.quarantined || 0}</div></div>
    <div class=\"kpi\"><div class=\"muted\">Devices actifs</div><div class=\"n\">${s.devices_active || 0}</div></div>
    <div class=\"kpi\"><div class=\"muted\">Devices révoqués</div><div class=\"n\">${s.devices_revoked || 0}</div></div>
    <div class=\"kpi\"><div class=\"muted\">Transcriptions OK</div><div class=\"n\">${s.transcription_completed}</div></div>
    <div class=\"kpi\"><div class=\"muted\">Transcriptions en cours</div><div class=\"n\">${s.transcription_processing}</div></div>
    <div class=\"kpi\"><div class=\"muted\">Transcriptions KO</div><div class=\"n\">${s.transcription_failed}</div></div>
  `;
}

function renderDevices(devices) {
  const root = document.getElementById('devices');
  if (!devices || !devices.length) {
    root.innerHTML = '<div class=\"muted\">Aucun device enrôlé.</div>';
    return;
  }
  root.innerHTML = devices.map(d => `
    <div class=\"obj\">
      <div style=\"display:flex;justify-content:space-between;align-items:center;gap:8px\">
        <div>
          <strong>${esc(d.device_name || 'Device sans nom')}</strong>
          <span class=\"pill\">${esc(d.status)}</span>
          <span class=\"pill\">code ${esc(d.simple_code || '-')}</span>
        </div>
        <button style=\"font-size:12px;padding:3px 7px;border:1px solid #fca5a5;border-radius:8px;background:#fff;color:#b91c1c;cursor:pointer\"
                onclick=\"revokeOneDevice('${esc(d.device_id)}')\">Révoquer</button>
      </div>
      <div class=\"muted\">last_seen: ${esc(d.last_seen_at || '-')} | retention: ${esc(d.retention_expires_at || '-')}</div>
      <div class=\"muted\">user_sub: ${esc(d.user_sub || '-')}</div>
    </div>
  `).join('');
}

function renderS3(s3) {
  const summaryEl = document.getElementById('s3-summary');
  const bucketsEl = document.getElementById('s3-buckets');
  if (!s3 || !s3.buckets) {
    summaryEl.textContent = 'S3 indisponible';
    bucketsEl.innerHTML = '';
    return;
  }

  const sum = s3.summary || {};
  summaryEl.textContent = `${sum.healthy_buckets || 0}/${sum.bucket_count || 0} buckets accessibles | ${sum.sample_objects || 0} objets (echantillon) | ${sum.sample_total_size_human || '0 B'}`;

  bucketsEl.innerHTML = (s3.buckets || []).map(b => {
    const objects = (b.objects || []).map(o => `
      <div class=\"obj\">
        <div><strong>${esc(o.key)}</strong></div>
        <div class=\"muted\">${esc(o.size_human)} | ${esc(o.last_modified || '-')} | <a href=\"api/s3/download?bucket=${encodeURIComponent(b.name)}&key=${encodeURIComponent(o.key)}\">Download</a></div>
      </div>
    `).join('');
    return `
      <div class=\"bucket\">
        <div>
          <span class=\"pill\">${esc(b.name)}</span>
          <span class=\"pill\">${esc(b.bucket)}</span>
          <span class=\"pill\">${esc(b.status)}</span>
        </div>
        <div class=\"muted\">endpoint: ${esc(b.endpoint)} | objets (echantillon): ${esc(b.sample_count)} | taille: ${esc(b.sample_size_human)}</div>
        ${b.error ? `<div class=\"muted warn\">${esc(b.error)}</div>` : ''}
        ${objects || '<div class=\"muted\">Aucun objet.</div>'}
      </div>
    `;
  }).join('');
}

function renderSessions(data) {
  const root = document.getElementById('sessions');
  if (!data.length) { root.innerHTML = '<div class=\"muted\">Aucune session.</div>'; return; }

  root.innerHTML = data.map(s => {
    const files = (s.files || []).map(f => `
      <div class=\"file\">
        <div><strong class=\"file-name\">${esc(f.name)}</strong></div>
        <div>
          <span class=\"pill\">pipeline: ${esc(f.status)}</span>
          <span class=\"pill\">stt: ${esc(f.transcription_status)}</span>
        </div>
        <div class=\"muted\">${esc(f.status_message)}</div>
        ${f.normalization_impact ? `
          <div class=\"muted\">
            normalisation: LUFS ${esc(f.normalization_impact.source.i)} -> ${esc(f.normalization_impact.normalized.i)}
            (delta ${esc(f.normalization_impact.delta.i)}),
            TP ${esc(f.normalization_impact.source.tp)} -> ${esc(f.normalization_impact.normalized.tp)},
            LRA ${esc(f.normalization_impact.source.lra)} -> ${esc(f.normalization_impact.normalized.lra)},
            gain cible -16 LUFS: ${esc(f.normalization_impact.improvement_to_target_lufs)}
          </div>
        ` : (f.normalization_error === 'pending_compute'
          ? `<div class=\"muted\">normalisation: analyse en attente...</div>`
          : (f.normalization_error === 'not_transcoded'
            ? `<div class=\"muted\">normalisation: disponible apres transcodage</div>`
            : (f.normalization_error === 'compute_failed'
              ? `<div class=\"muted warn\">normalisation: analyse indisponible</div>`
              : '')))}
        ${f.transcription_preview ? `<div class=\"muted\">transcription: ${esc(f.transcription_preview)}</div>` : ''}
        ${f.internal_key ? `
          <div class=\"file-actions muted\">
            <a href=\"api/s3/download?bucket=${encodeURIComponent(f.internal_bucket)}&key=${encodeURIComponent(f.internal_key)}\">Telecharger</a>
            <a href=\"#\" onclick=\"purgeOneFile('${esc(f.file_id)}','${esc((f.name || '').replace(/'/g, '’'))}'); return false;\" style=\"color:#b42318\">Purger ce fichier</a>
          </div>
          <audio controls preload=\"none\" src=\"api/s3/stream?bucket=${encodeURIComponent(f.internal_bucket)}&key=${encodeURIComponent(f.internal_key)}\"></audio>
        ` : `
          <div class=\"file-actions muted\">
            <a href=\"#\" onclick=\"purgeOneFile('${esc(f.file_id)}','${esc((f.name || '').replace(/'/g, '’'))}'); return false;\" style=\"color:#b42318\">Purger ce fichier</a>
          </div>
          <div class=\"muted\">audio non disponible (pas encore transfere en zone interne)</div>
        `}
      </div>
    `).join('');

    return `
      <div class=\"session\">
        <div>
          <span class=\"pill\">code ${esc(s.simple_code)}</span>
          <span class=\"pill\">${esc(s.upload_count)}/${esc(s.max_uploads)} fichiers</span>
          <span class=\"pill\">${esc(s.status)}</span>
        </div>
        <div class=\"muted\">${esc(s.user_email || '-')} | cree: ${esc(s.created_at)} | expire: ${esc(s.expires_at)}</div>
        <div class=\"files\">${files || '<div class=\"muted warn\">Pas de fichier sur cette session.</div>'}</div>
      </div>
    `;
  }).join('');
}

function renderTranscriptionEvents(events) {
  const root = document.getElementById('stt-events');
  if (!events || !events.length) {
    root.innerHTML = '<div class=\"muted\">Aucun evenement.</div>';
    return;
  }
  root.innerHTML = events.map(e => {
    const md = e.metadata || {};
    const details = Object.keys(md).length ? esc(JSON.stringify(md)) : '-';
    return `
      <div class=\"obj\">
        <div><strong>${esc(e.event_type)}</strong> | code: ${esc(e.session_code || '-')} | ${esc(e.created_at || '-')}</div>
        <div class=\"muted\">${esc(e.message || '')}</div>
        <div class=\"muted\">metadata: ${details}</div>
      </div>
    `;
  }).join('');
}

async function purgeOneFile(fileId, fileName) {
  if (!fileId) return;
  const ok = window.confirm(`Supprimer definitivement le fichier: ${fileName || fileId} ?`);
  if (!ok) return;
  try {
    const r = await fetch('api/purge-file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_id: fileId }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'purge_failed');
    await loadData();
  } catch (e) {
    alert('Echec purge fichier.');
  }
}

async function purgeAllFiles() {
  const ok = window.confirm('Supprimer tous les fichiers (S3 + DB) ?');
  if (!ok) return;
  const btn = document.getElementById('purge-all-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Purge en cours...';
  }
  try {
    const r = await fetch('api/purge-all-files', { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'purge_all_failed');
    await loadData();
  } catch (e) {
    alert('Echec purge globale.');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Purger tous les fichiers';
    }
  }
}

async function revokeOneDevice(deviceId) {
  if (!deviceId) return;
  const ok = window.confirm('Révoquer ce device ?');
  if (!ok) return;
  try {
    const r = await fetch('api/devices/revoke-one', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'device_revoke_failed');
    await loadData();
  } catch (e) {
    alert('Echec révocation device.');
  }
}

async function revokeAllDevices() {
  const ok = window.confirm('Révoquer tous les devices enrôlés ?');
  if (!ok) return;
  try {
    const r = await fetch('api/devices/revoke-all', { method: 'POST' });
    const data = await r.json();
    if (!r.ok || !data.ok) throw new Error(data.error || 'device_revoke_all_failed');
    await loadData();
  } catch (e) {
    alert('Echec révocation globale devices.');
  }
}

async function loadData() {
  if (window.__dashboardInFlight) return;
  window.__dashboardInFlight = true;
  try {
    const hasActivePlayback = Array.from(document.querySelectorAll('audio'))
      .some(a => !a.paused && !a.ended);
    if (hasActivePlayback) {
      window.__dashboardInFlight = false;
      return;
    }
    const r = await fetch('api/dashboard');
    if (r.status === 401) {
      window.location.href = 'login';
      return;
    }
    if (r.status === 403) throw new Error('forbidden');
    if (!r.ok) throw new Error('api error');
    const data = await r.json();
    if (data.error === 'dashboard_unavailable') {
      document.getElementById('sessions').innerHTML = '<div class=\"muted warn\">Dashboard temporairement indisponible. Recharge la page.</div>';
      document.getElementById('devices').innerHTML = '<div class=\"muted warn\">Devices temporairement indisponibles.</div>';
      document.getElementById('s3-buckets').innerHTML = '<div class=\"muted warn\">S3 temporairement indisponible.</div>';
      document.getElementById('stt-events').innerHTML = '<div class=\"muted warn\">Journal transcription temporairement indisponible.</div>';
      return;
    }
    renderSummary(data.summary || {});
    renderDevices(data.devices || []);
    renderS3(data.s3 || {});
    renderTranscriptionEvents(data.transcription_events || []);
    renderSessions(data.sessions || []);
  } catch (e) {
    document.getElementById('sessions').innerHTML = '<div class=\"muted warn\">Erreur chargement dashboard.</div>';
    document.getElementById('devices').innerHTML = '<div class=\"muted warn\">Erreur chargement devices.</div>';
    document.getElementById('s3-buckets').innerHTML = '<div class=\"muted warn\">Erreur chargement S3.</div>';
    document.getElementById('stt-events').innerHTML = '<div class=\"muted warn\">Erreur chargement journal transcription.</div>';
  } finally {
    window.__dashboardInFlight = false;
  }
}

loadData();
document.getElementById('purge-all-btn')?.addEventListener('click', purgeAllFiles);
document.getElementById('revoke-all-devices-btn')?.addEventListener('click', revokeAllDevices);
let dashboardTimer = setInterval(loadData, 5000);
document.getElementById('logout-link')?.addEventListener('click', () => {
  if (dashboardTimer) {
    clearInterval(dashboardTimer);
    dashboardTimer = null;
  }
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.getenv("ADMIN_PORTAL_PORT", "8082"))
    application.run(host="0.0.0.0", port=port, debug=False)
