"""Blueprint ``sessions`` — endpoints uploads, fichiers, corbeille, status.

PR3-v2 : extraction depuis ``main.py``. Routes restées sous leurs URLs
canoniques pour ne rien casser côté front (préservation API).
"""

from __future__ import annotations

import logging
import os
import random
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import requests as req
from flask import Blueprint, abort, jsonify, request, send_file, session

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
from libs.shared.app.config import (  # noqa: E402
    ALLOWED_AUDIO_EXTENSIONS, INTERNAL_API_TOKEN,
    UPLOAD_STATUS_VIEW_TTL_MINUTES,
)
from libs.shared.app.models import (  # noqa: E402
    SessionStatus, UploadSession, UploadStatus, UploadedFile,
)
from libs.shared.app.s3_helper import download_fileobj, delete_object, object_exists  # noqa: E402
from libs.shared.app.security import verify_bearer_token  # noqa: E402
from libs.shared.app.database import with_db_retry  # noqa: E402
from libs.shared.app.upload_helpers import (  # noqa: E402
    build_stored_filename, is_allowed_audio_filename, publish_av_scan_message,
    store_audio_to_s3,
)

from app.runtime import (
    get_rabbit_cfg, get_s3_upload_cfg, get_s3_processed_cfg,
    session_scope,
)
from app.shared import (
    get_current_user, require_auth, request_internal_device_api,
)

from . import service as svc

logger = logging.getLogger("mesreunions_web.sessions.routes")

bp = Blueprint("sessions", __name__)


# Helper local : appel internal-ingester (utilisé par /api/my-sessions
# pour bulk meeting-datetimes et /api/queue-status).
def _request_internal_ingester_api(path, *, json_body=None, params=None,
                                    method="POST", timeout=10):
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL",
                     "http://internal-ingester:8090").rstrip("/")
    resp = req.request(
        method, f"{base}{path}",
        json=json_body, params=params,
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


def _audio_or_404(db, user_sub, file_id):
    file_obj = svc.get_owned_file(db, user_sub, file_id)
    if not file_obj:
        abort(404, "File not found")
    return file_obj, svc.lookup_audio_outputs(db, file_obj)


def _send_text_attachment(text, filename, mime="text/plain"):
    import json as _json
    if not isinstance(text, str):
        text = _json.dumps(text, ensure_ascii=False, indent=2)
    return send_file(
        BytesIO(text.encode("utf-8")),
        mimetype=mime, as_attachment=True, download_name=filename,
    )


# ─── /api/my-sessions ────────────────────────────────────────────────────


_PURGE_TRASH_PROBABILITY = float(os.getenv("MY_SESSIONS_PURGE_PROBABILITY", "0.1"))
_purge_inflight_users = set()
_purge_inflight_lock = threading.Lock()


def _async_purge_expired_trash(user_sub: str):
    """Lance la purge corbeille en arrière-plan, dans sa propre session DB.

    Lock par user_sub pour qu'on ne fasse pas tourner deux purges du même
    user en parallèle si deux requêtes arrivent ensemble. La purge est
    déclenchée probabilistiquement (cf MY_SESSIONS_PURGE_PROBABILITY)
    pour ne pas saturer les ressources sur un user qui fait du polling
    serré — un crons ou un déclenchement 1 fois sur 10 suffit largement
    pour un seuil de 30 jours.
    """
    with _purge_inflight_lock:
        if user_sub in _purge_inflight_users:
            return
        _purge_inflight_users.add(user_sub)

    def _run():
        db2 = session_scope()
        try:
            svc.purge_expired_trash(db2, user_sub)
            db2.commit()
        except Exception:
            try: db2.rollback()
            except Exception: pass
            logger.debug("background trash purge failed for %s", user_sub, exc_info=True)
        finally:
            try: db2.close()
            except Exception: pass
            with _purge_inflight_lock:
                _purge_inflight_users.discard(user_sub)

    t = threading.Thread(target=_run, name=f"trash-purge-{user_sub[:8]}", daemon=True)
    t.start()


@bp.route("/api/my-sessions")
@require_auth
def api_my_sessions():
    user = get_current_user()
    db = session_scope()
    s3_upload_cfg = get_s3_upload_cfg()
    s3_processed_cfg = get_s3_processed_cfg()
    timings = {}
    _t0 = time.monotonic()
    try:
        # Purge corbeille → hors hot path : déclenchée probabilistiquement
        # dans un thread background avec sa propre session DB. Auparavant
        # synchronisée elle pesait 300-1000ms (2 calls cross-cluster +
        # S3 deletes inline) sur CHAQUE chargement de la liste.
        if random.random() < _PURGE_TRASH_PROBABILITY:
            _async_purge_expired_trash(user["sub"])

        # Les deux appels internes (ingester + device) + la query DB sont
        # tous indépendants. On les lance en parallèle pour éliminer la
        # séquentialité résiduelle (gain ~100-150ms en typique).
        def _fetch_meeting_dt():
            try:
                return _request_internal_ingester_api(
                    "/api/v1/audio/meeting-datetimes",
                    method="GET", params={"user_sub": user["sub"]},
                )
            except Exception:
                logger.debug("meeting-datetimes bulk fetch failed (non-fatal)", exc_info=True)
                return None

        def _fetch_devices():
            try:
                return request_internal_device_api(
                    "GET", "/api/v1/devices",
                    params={"user_sub": user.get("sub", "")},
                )
            except Exception:
                logger.debug("Could not fetch devices for lifecycle enrichment", exc_info=True)
                return None

        with ThreadPoolExecutor(max_workers=2) as _pool:
            _bulk_future = _pool.submit(_fetch_meeting_dt)
            _devices_future = _pool.submit(_fetch_devices)
            # En parallèle des appels externes, on lance la query DB
            # principale. SQLAlchemy n'est pas thread-safe sur une même
            # session : on garde donc la query dans le thread Flask et
            # on n'attend les futures qu'après.
            sessions = db.query(UploadSession).filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.is_(None),
            ).order_by(UploadSession.created_at.desc()).limit(20).all()
            bulk = _bulk_future.result()
            devices = _devices_future.result()
        timings["t1_parallel_fetch"] = round((time.monotonic() - _t0) * 1000)

        meeting_dt_overrides = {}
        if isinstance(bulk, dict):
            for it in (bulk.get("items") or []):
                code = (it.get("simple_code") or "").strip()
                name = (it.get("original_filename") or "").strip()
                dt = it.get("meeting_datetime")
                if code and name and dt:
                    meeting_dt_overrides[(code, name)] = dt

        active_qr_tokens = set()
        now = datetime.now(timezone.utc)
        for d in (devices if isinstance(devices, list) else []):
            if not isinstance(d, dict):
                continue
            if (d.get("status") or "").lower() != "active":
                continue
            retention_raw = d.get("retention_expires_at")
            if retention_raw:
                try:
                    retention = datetime.fromisoformat(retention_raw.replace("Z", "+00:00"))
                    if retention.tzinfo is None:
                        retention = retention.replace(tzinfo=timezone.utc)
                    if retention <= now:
                        continue
                except Exception:
                    pass
            qr = (d.get("qr_token") or "").strip()
            if qr:
                active_qr_tokens.add(qr)

        # ── Batch S3 HEAD probes ────────────────────────────────────────
        # Auparavant on enchaînait 1 à 4 ``object_exists`` synchrones par
        # fichier : pour ~20 sessions × 5 fichiers × 3 probes c'était
        # ~300 HEAD séquentiels. On précalcule la liste de probes puis on
        # tape S3 en parallèle (un seul ThreadPoolExecutor borné).
        def _safe_exists(cfg, key):
            try:
                return object_exists(cfg, key)
            except Exception:
                logger.debug("object_exists failed for %s", key, exc_info=True)
                return False

        probe_specs = {}  # marker → (cfg, key)
        transferred_storage_cache = {}  # file_id → (cfg, key) or (None, None)

        def _transferred_storage(f):
            if f.id not in transferred_storage_cache:
                try:
                    transferred_storage_cache[f.id] = svc.resolve_transferred_storage(db, f)
                except Exception:
                    transferred_storage_cache[f.id] = (None, None)
            return transferred_storage_cache[f.id]

        # On ne probe QUE les emplacements utilisés par l'UI principale :
        # ``transferred`` (bouton "Écouter audio interne", la version
        # canonique) + ``reconcile`` (rattrapage de statut transitoire).
        # Les variantes ``source`` (brut DMZ) et ``transcoded`` (normalisé
        # DMZ) sont des artefacts de debug exposés uniquement en mode
        # avancé du menu "Autres" ; elles sont sondées à la demande via
        # /api/file/<id>/audio-availability quand l'utilisateur déplie
        # ce menu. Côté ``/api/my-sessions`` on retourne donc une
        # présomption basée sur la présence du filename en DB — c'est
        # suffisant pour griser le bouton si l'enregistrement DB n'a
        # jamais eu cette variante, et acceptable côté UX si un fichier
        # purgé S3 mais encore en DB renvoie un 404 au clic (cas rare).
        for s in sessions:
            for f in s.uploads:
                if f.trashed_at is not None:
                    continue
                if f.status == UploadStatus.TRANSFERRED and f.transcoded_filename:
                    t_cfg, t_key = _transferred_storage(f)
                    if t_cfg and t_key:
                        probe_specs[("transferred", f.id)] = (t_cfg, t_key)
                # Rattrapage de statut : on a besoin du résultat aussi.
                if f.status in {UploadStatus.READY_FOR_TRANSFER, UploadStatus.TRANSFERRING} and f.transcoded_filename:
                    t_cfg, t_key = _transferred_storage(f)
                    if t_cfg and t_key:
                        probe_specs[("reconcile", f.id)] = (t_cfg, t_key)

        probe_results = {}
        if probe_specs:
            # max_workers borné pour ne pas saturer le pool S3 si un user
            # a beaucoup de fichiers ; 32 ≈ ~10× speed-up sans risque.
            with ThreadPoolExecutor(max_workers=min(32, len(probe_specs))) as _pool:
                futures = {
                    _pool.submit(_safe_exists, cfg, key): marker
                    for marker, (cfg, key) in probe_specs.items()
                }
                for fut in futures:
                    probe_results[futures[fut]] = fut.result()
        timings["t2_s3_probes"] = round((time.monotonic() - _t0) * 1000)
        timings["_probe_count"] = len(probe_specs)

        reconciled = 0
        result = []
        for s in sessions:
            uploads = []
            for f in s.uploads:
                if f.trashed_at is not None:
                    continue
                if f.status in {UploadStatus.READY_FOR_TRANSFER, UploadStatus.TRANSFERRING} and f.transcoded_filename:
                    if probe_results.get(("reconcile", f.id)):
                        f.status = UploadStatus.TRANSFERRED
                        f.status_message = "Fichier intégré à votre compte. Transcription en cours... (rattrapage auto)"
                        if not f.transferred_at:
                            f.transferred_at = datetime.now(timezone.utc)
                        reconciled += 1

                # Présomption optimiste pour les variantes secondaires :
                # tant que le filename existe en DB, on présume que le
                # blob S3 est encore là. Le HEAD réel n'est fait qu'à la
                # demande (audio-availability) ou implicitement au clic.
                source_available = bool(f.stored_filename)
                transcoded_available = bool(f.transcoded_filename)
                transferred_available = False
                if f.status == UploadStatus.TRANSFERRED and f.transcoded_filename:
                    transferred_available = bool(probe_results.get(("transferred", f.id)))

                override_dt = meeting_dt_overrides.get((s.simple_code, f.original_filename))
                uploads.append({
                    "id": str(f.id),
                    "original_filename": f.original_filename,
                    "status": f.status.value,
                    "status_message": f.status_message,
                    "audio_quality_score": f.audio_quality_score,
                    "audio_duration_seconds": f.audio_duration_seconds,
                    "created_at": f.created_at.isoformat(),
                    "updated_at": f.updated_at.isoformat() if f.updated_at else None,
                    "meeting_datetime": override_dt,
                    "meeting_datetime_overridden": override_dt is not None,
                    "download_url": f"/api/file/download/{f.id}",
                    "stream_url": f"/api/file/stream/{f.id}",
                    "source_available": source_available,
                    "source_download_url": f"/api/file/download-source/{f.id}" if source_available else None,
                    "source_stream_url": f"/api/file/stream-source/{f.id}" if source_available else None,
                    "transcoded_available": transcoded_available,
                    "transcoded_download_url": f"/api/file/download-transcoded/{f.id}" if transcoded_available else None,
                    "transcoded_stream_url": f"/api/file/stream-transcoded/{f.id}" if transcoded_available else None,
                    "transferred_available": transferred_available,
                    "transferred_download_url": f"/api/file/download-transferred/{f.id}" if transferred_available else None,
                    "transferred_stream_url": f"/api/file/stream-transferred/{f.id}" if transferred_available else None,
                    "impact_url": f"/api/file/normalization-impact/{f.id}",
                })
            is_local = (s.simple_code or "").startswith(svc.LOCAL_UPLOAD_SIMPLE_CODE_PREFIX)
            result.append({
                "id": str(s.id),
                "simple_code": s.simple_code,
                "qr_token": s.qr_token,
                "status": s.status.value,
                "upload_count": s.upload_count,
                "max_uploads": s.max_uploads,
                "expires_at": s.expires_at.isoformat(),
                "created_at": s.created_at.isoformat(),
                "lifecycle_state": svc.compute_lifecycle_state(
                    s, has_active_device=(s.qr_token or "") in active_qr_tokens,
                ),
                "is_local_upload": is_local,
                "device_label": svc.LOCAL_UPLOAD_DEVICE_LABEL if is_local else None,
                "uploads": uploads,
            })
        if reconciled:
            db.commit()
            logger.info("Auto-reconciled %s transfer status entries for user %s", reconciled, user.get("sub"))
        timings["t3_total"] = round((time.monotonic() - _t0) * 1000)
        timings["_session_count"] = len(sessions)
        # Log timing pour identifier les régressions de perf sur le hot
        # path. INFO car la fiche de bord du backend en a besoin pour
        # alerter sur les SLO dégradés ; reste lisible côté Loki.
        logger.info(
            "my-sessions timing user=%s sessions=%s probes=%s parallel=%sms s3=%sms total=%sms",
            user.get("sub", "?")[:12], timings["_session_count"], timings["_probe_count"],
            timings["t1_parallel_fetch"], timings["t2_s3_probes"], timings["t3_total"],
        )
        return jsonify(result)
    finally:
        db.close()


# ─── Downloads ──────────────────────────────────────────────────────────


@bp.route("/api/file/download/<file_id>")
@require_auth
def api_file_download(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_file_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(data, mimetype=file_obj.mime_type or "application/octet-stream",
                         as_attachment=True, download_name=file_obj.original_filename)
    finally:
        db.close()


@bp.route("/api/file/stream/<file_id>")
@require_auth
def api_file_stream(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_file_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(data, mimetype=file_obj.mime_type or "audio/wav",
                         as_attachment=False, download_name=file_obj.original_filename)
    finally:
        db.close()


@bp.route("/api/file/download-source/<file_id>")
@require_auth
def api_file_download_source(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_source_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(data, mimetype=file_obj.mime_type or "application/octet-stream",
                         as_attachment=True, download_name=file_obj.original_filename)
    finally:
        db.close()


@bp.route("/api/file/stream-source/<file_id>")
@require_auth
def api_file_stream_source(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_source_storage(file_obj)
        data = download_fileobj(cfg, key)
        return send_file(data, mimetype=file_obj.mime_type or "audio/*",
                         as_attachment=False, download_name=file_obj.original_filename)
    finally:
        db.close()


@bp.route("/api/file/download-transcoded/<file_id>")
@require_auth
def api_file_download_transcoded(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_transcoded_storage(file_obj)
        if not cfg or not key:
            abort(404, "Transcoded file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(data, mimetype=svc.guess_audio_mime_from_key(key),
                         as_attachment=True,
                         download_name=f"{Path(file_obj.original_filename).stem}_transcoded{suffix}")
    finally:
        db.close()


@bp.route("/api/file/stream-transcoded/<file_id>")
@require_auth
def api_file_stream_transcoded(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_transcoded_storage(file_obj)
        if not cfg or not key:
            abort(404, "Transcoded file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(data, mimetype=svc.guess_audio_mime_from_key(key),
                         as_attachment=False,
                         download_name=f"{Path(file_obj.original_filename).stem}_transcoded{suffix}")
    finally:
        db.close()


@bp.route("/api/file/download-transferred/<file_id>")
@require_auth
def api_file_download_transferred(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_transferred_storage(db, file_obj)
        if not cfg or not key:
            abort(404, "Transferred file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(data, mimetype=svc.guess_audio_mime_from_key(key),
                         as_attachment=True,
                         download_name=f"{Path(file_obj.original_filename).stem}_transferred{suffix}")
    finally:
        db.close()


@bp.route("/api/file/stream-transferred/<file_id>")
@require_auth
def api_file_stream_transferred(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        cfg, key = svc.resolve_transferred_storage(db, file_obj)
        if not cfg or not key:
            abort(404, "Transferred file not available")
        data = download_fileobj(cfg, key)
        suffix = Path(key).suffix or ".bin"
        return send_file(data, mimetype=svc.guess_audio_mime_from_key(key),
                         as_attachment=False,
                         download_name=f"{Path(file_obj.original_filename).stem}_transferred{suffix}")
    finally:
        db.close()


# ─── Transcript / CR downloads ──────────────────────────────────────────


@bp.route("/api/file/transcript/<kind>/<ext>/<file_id>")
@require_auth
def api_file_transcript_download(kind, ext, file_id):
    if kind not in svc.TRANSCRIPT_KIND_TO_COLUMN:
        abort(404)
    if ext not in ("txt", "md", "docx", "odt"):
        abort(404)
    user = get_current_user()
    db = session_scope()
    try:
        file_obj, audio = _audio_or_404(db, user["sub"], file_id)
        if audio is None:
            return jsonify({"error": "transcript_not_ready"}), 503
        column = svc.TRANSCRIPT_KIND_TO_COLUMN[kind]
        text = audio.get(column)
        if not text:
            return jsonify({"error": f"{kind}_unavailable"}), 410
        stem = svc.build_download_basename(file_obj, audio, kind)
        from app.transcript_formats import (
            text_to_docx_bytes, text_to_odt_bytes,
            text_to_plain_string, text_to_md_string,
            build_document_header_md,
            extract_speakers, split_reformulated_by_speaker,
        )
        # Discours indirect (reformulated) : on insère un retour à la
        # ligne avant chaque transition de locuteur, en se basant sur la
        # liste des speakers extraite du speaker-tagged.
        if kind == "transcript-reformulated":
            speakers = extract_speakers(audio.get("speaker_tagged_text") or "")
            text = split_reformulated_by_speaker(text, speakers)
        # En-tête commun aux 4 formats : titre + 🇫🇷 République Française
        # + date + durée + points clés (les key_points ne sont plus
        # prepended séparément, c'est notre header qui les inclut).
        meeting_dt_iso = None
        try:
            meeting_dt_iso = file_obj.meeting_datetime.isoformat() if getattr(file_obj, "meeting_datetime", None) else None
        except Exception:
            meeting_dt_iso = None
        upload_dt_iso = file_obj.created_at.isoformat() if file_obj.created_at else None
        header_md = build_document_header_md(
            title=stem,
            kind=kind,
            meeting_date_iso=meeting_dt_iso,
            upload_date_iso=upload_dt_iso,
            duration_seconds=file_obj.audio_duration_seconds,
            key_points=audio.get("key_points_summary"),
        )
        body = header_md + text
        if ext == "txt":
            # Strip MD inline + normalise blank lines + sépare locuteurs.
            return _send_text_attachment(
                text_to_plain_string(body), f"{stem}.txt",
                "text/plain; charset=utf-8",
            )
        if ext == "md":
            return _send_text_attachment(
                text_to_md_string(body), f"{stem}.md",
                "text/markdown; charset=utf-8",
            )
        if ext == "docx":
            blob = text_to_docx_bytes(body, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             as_attachment=True, download_name=f"{stem}.docx")
        if ext == "odt":
            blob = text_to_odt_bytes(body, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.oasis.opendocument.text",
                             as_attachment=True, download_name=f"{stem}.odt")
    finally:
        db.close()


@bp.route("/api/file/meeting-cr/<ext>/<file_id>")
@require_auth
def api_file_meeting_cr_download(ext, file_id):
    if ext not in ("json", "md", "docx", "odt"):
        abort(404)
    user = get_current_user()
    db = session_scope()
    try:
        file_obj, audio = _audio_or_404(db, user["sub"], file_id)
        if audio is None:
            return jsonify({"error": "transcript_not_ready"}), 503
        raw = audio.get("meeting_analysis_json")
        if not raw:
            return jsonify({"error": "meeting_cr_unavailable"}), 410
        stem = svc.build_download_basename(file_obj, audio, "meeting-cr")
        if ext == "json":
            return _send_text_attachment(raw, f"{stem}.json", "application/json; charset=utf-8")
        from app.transcript_formats import (
            meeting_analysis_to_markdown, text_to_docx_bytes, text_to_odt_bytes,
            text_to_md_string, build_document_header_md,
        )
        md = meeting_analysis_to_markdown(raw)
        # Préfixe le contenu meeting-cr par l'en-tête commun
        # (titre + RF + date + durée + points clés).
        meeting_dt_iso = None
        try:
            meeting_dt_iso = file_obj.meeting_datetime.isoformat() if getattr(file_obj, "meeting_datetime", None) else None
        except Exception:
            meeting_dt_iso = None
        upload_dt_iso = file_obj.created_at.isoformat() if file_obj.created_at else None
        header_md = build_document_header_md(
            title=stem,
            kind="meeting-cr",
            meeting_date_iso=meeting_dt_iso,
            upload_date_iso=upload_dt_iso,
            duration_seconds=file_obj.audio_duration_seconds,
            key_points=audio.get("key_points_summary"),
        )
        md = header_md + md
        if ext == "md":
            return _send_text_attachment(
                text_to_md_string(md), f"{stem}.md",
                "text/markdown; charset=utf-8",
            )
        if ext == "docx":
            blob = text_to_docx_bytes(md, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             as_attachment=True, download_name=f"{stem}.docx")
        if ext == "odt":
            blob = text_to_odt_bytes(md, title=stem)
            return send_file(BytesIO(blob),
                             mimetype="application/vnd.oasis.opendocument.text",
                             as_attachment=True, download_name=f"{stem}.odt")
    finally:
        db.close()


# Champs texte volumineux qu'on omet du payload "fiche détaillée initiale"
# (mode ``?summary=1``) pour rendre la page rapide. On garde
# ``meeting_analysis_json`` car le premier clic ouvre le compte-rendu, et
# les autres textes sont chargés en arrière-plan ou à la demande via
# ``/api/file/transcript-text``.
_HEAVY_TRANSCRIPT_FIELDS = (
    "speaker_tagged_text",
    "cleaned_text",
    "reformulated_text",
    "absentee_summary",
)


@bp.route("/api/file/transcript-status/<file_id>")
@require_auth
def api_file_transcript_status(file_id):
    user = get_current_user()
    summary_only = (request.args.get("summary") or "").strip() in ("1", "true", "yes")
    # Lookup wrapped in with_db_retry pour absorber les "server closed the
    # connection unexpectedly" sporadiques (bug routing inter-cluster SCW
    # LB postgres-external-lb depuis internal-gw — cf with_db_retry doc).
    def _lookup():
        db = session_scope()
        try:
            return _audio_or_404(db, user["sub"], file_id), db
        except Exception:
            try: db.close()
            except Exception: pass
            raise
    (file_obj, audio), db = with_db_retry(_lookup, max_attempts=3)
    try:
        if audio is None:
            return jsonify({"available": False, "reason": "not_ready"})
        flags = {k: bool(audio.get(col)) for k, col in svc.TRANSCRIPT_KIND_TO_COLUMN.items()}
        flags["meeting-cr"] = bool(audio.get("meeting_analysis_json"))
        # Indique au front si l'absentee_summary existe sans avoir à le
        # télécharger (mode summary). Permet de décider la visibilité de
        # l'onglet « Pour les absents » avant le lazy-load.
        flags["absentee"] = bool(audio.get("absentee_summary"))
        payload = {
            "available": True,
            "summary_only": summary_only,
            "transcription_status": audio.get("transcription_status"),
            "transcription_engine": audio.get("transcription_engine"),
            "transcription_language": audio.get("transcription_language"),
            "outputs": flags,
            "suggested_filename": audio.get("suggested_filename"),
            "key_points_summary": audio.get("key_points_summary"),
            "meeting_datetime": audio.get("meeting_datetime"),
            "kevent_job_id": audio.get("kevent_job_id"),
            # Le CR (meeting_analysis_json) reste exposé même en mode
            # summary : c'est le premier onglet, on veut éviter un round-trip
            # supplémentaire pour l'afficher.
            "meeting_analysis_json": audio.get("meeting_analysis_json"),
            "reprocess_version": audio.get("reprocess_version") or 0,
            "last_reprocessed_at": audio.get("last_reprocessed_at"),
        }
        if not summary_only:
            # Texte speaker-tagged (avec timecodes par bloc) exposé pour
            # la correction inline + ré-écoute audio par bloc speaker.
            payload["speaker_tagged_text"] = audio.get("speaker_tagged_text")
            # Textes CR pour rendu inline (markdown via marked.js côté
            # frontend) + corrector C avec drawer source-segments.
            payload["cleaned_text"] = audio.get("cleaned_text")
            payload["reformulated_text"] = audio.get("reformulated_text")
            payload["absentee_summary"] = audio.get("absentee_summary")
        return jsonify(payload)
    finally:
        db.close()


# Mapping kind exposé côté frontend → colonne UserAudioFile renvoyée par
# l'ingester. Volontairement restreint aux textes que les onglets / le
# corrector consomment, pour ne pas exposer plus que nécessaire.
_TRANSCRIPT_TEXT_KINDS = {
    "speaker_tagged": "speaker_tagged_text",
    "cleaned": "cleaned_text",
    "reformulated": "reformulated_text",
    "absentee": "absentee_summary",
    "meeting_analysis": "meeting_analysis_json",
    # Whisper brut sans diarisation — fallback du corrector quand le
    # speaker_tagged est vide (échec pyannote).
    "transcription": "transcription_text",
}


@bp.route("/api/file/transcript-text/<file_id>/<kind>")
@require_auth
def api_file_transcript_text(file_id, kind):
    """Charge un seul blob texte à la demande.

    Sert le lazy-load des onglets non-CR (reformulation / nettoyée /
    absentee) et du corrector (speaker_tagged). Tous les onglets de la
    fiche détaillée peuvent ainsi se charger en arrière-plan après le
    premier render, ou à la demande au moment du clic.
    """
    column = _TRANSCRIPT_TEXT_KINDS.get(kind)
    if not column:
        abort(404, "Unknown transcript kind")
    user = get_current_user()
    def _lookup():
        db = session_scope()
        try:
            return _audio_or_404(db, user["sub"], file_id), db
        except Exception:
            try: db.close()
            except Exception: pass
            raise
    (file_obj, audio), db = with_db_retry(_lookup, max_attempts=3)
    try:
        if audio is None:
            return jsonify({"available": False, "reason": "not_ready"})
        return jsonify({
            "available": True,
            "kind": kind,
            "text": audio.get(column),
        })
    finally:
        db.close()


# ─── Audio availability (lazy probe pour source/transcoded) ────────────


@bp.route("/api/file/<file_id>/audio-availability")
@require_auth
def api_file_audio_availability(file_id):
    """Sonde S3 à la demande pour les 3 variantes audio d'un fichier.

    Pas appelé sur /api/my-sessions (qui ne probe que ``transferred`` pour
    rester rapide) — utilisé par le front quand l'utilisateur déplie le
    menu "Autres" en mode avancé et qu'on veut savoir si les boutons
    ``source`` / ``transcoded`` doivent être grisés. Coût : 1-3 HEAD S3
    en parallèle, équivalent au plus à ~50ms avec le client boto3 caché.
    """
    user = get_current_user()
    db = session_scope()
    s3_upload_cfg = get_s3_upload_cfg()
    s3_processed_cfg = get_s3_processed_cfg()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")

        def _safe_exists(cfg, key):
            try: return object_exists(cfg, key)
            except Exception: return False

        probes = {}
        if file_obj.stored_filename:
            probes["source"] = (s3_upload_cfg, file_obj.stored_filename)
        if file_obj.transcoded_filename:
            probes["transcoded"] = (s3_processed_cfg, file_obj.transcoded_filename)
        if file_obj.status == UploadStatus.TRANSFERRED and file_obj.transcoded_filename:
            try:
                t_cfg, t_key = svc.resolve_transferred_storage(db, file_obj)
                if t_cfg and t_key:
                    probes["transferred"] = (t_cfg, t_key)
            except Exception:
                logger.debug("resolve_transferred_storage failed for %s", file_id, exc_info=True)

        results = {}
        if probes:
            with ThreadPoolExecutor(max_workers=len(probes)) as _pool:
                futures = {_pool.submit(_safe_exists, cfg, key): kind for kind, (cfg, key) in probes.items()}
                for fut in futures:
                    results[futures[fut]] = fut.result()
        return jsonify({
            "source_available": bool(results.get("source", False)),
            "transcoded_available": bool(results.get("transcoded", False)),
            "transferred_available": bool(results.get("transferred", False)),
        })
    finally:
        db.close()


# ─── Queue status ───────────────────────────────────────────────────────


@bp.route("/api/queue-status", methods=["GET"])
def api_queue_status():
    has_session = bool(session.get("user"))
    has_internal = verify_bearer_token(
        request.headers.get("Authorization", ""), INTERNAL_API_TOKEN
    )
    if not (has_session or has_internal):
        return jsonify({"error": "Unauthorized"}), 401
    job_id = (request.args.get("job_id") or "").strip()
    service_type = (request.args.get("service_type") or "audio").strip()
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL",
                     "http://internal-ingester:8090").rstrip("/")
    params = {"service_type": service_type}
    if job_id:
        params["job_id"] = job_id
    try:
        resp = req.get(
            f"{base}/api/v1/queue-status",
            headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
            params=params, timeout=6,
        )
        return jsonify(resp.json()), resp.status_code
    except Exception:
        logger.warning("queue-status proxy to internal-ingester failed", exc_info=True)
        return jsonify({
            "pending_total": None, "processing_total": None,
            "your_position": None, "eta_seconds": None,
            "throughput_per_min": None, "stale": True,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }), 503


# ─── Rename / meeting-datetime ──────────────────────────────────────────


@bp.route("/api/file/<file_id>/rename", methods=["POST"])
@require_auth
def api_rename_file(file_id):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    new_title = (payload.get("title") or "").strip()
    if not new_title:
        return jsonify({"error": "title is required"}), 400
    if len(new_title) > 500:
        return jsonify({"error": "title too long"}), 400

    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            return jsonify({"error": "file_not_found"}), 404
        session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
        if not session_obj:
            return jsonify({"error": "session_not_found"}), 404
        try:
            data = request_internal_device_api(
                "POST", "/api/v1/files/by-session/rename",
                json_body={
                    "user_sub": user["sub"],
                    "simple_code": session_obj.simple_code,
                    "original_filename": file_obj.original_filename,
                    "new_title": new_title,
                },
            )
        except req.HTTPError as err:
            if err.response is not None:
                try:
                    body = err.response.json()
                except Exception:
                    body = {"error": "internal_api_error"}
                return jsonify({"error": body.get("error", "rename_failed")}), err.response.status_code
            return jsonify({"error": "rename_failed"}), 502
        return jsonify({"ok": True, "title": data.get("new_title", new_title)})
    finally:
        db.close()


@bp.route("/api/file/<file_id>/meeting-datetime", methods=["PATCH"])
@require_auth
def api_set_meeting_datetime(file_id):
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    if "meeting_datetime" not in payload:
        return jsonify({"error": "meeting_datetime field required (string or null)"}), 400
    raw_dt = payload.get("meeting_datetime")
    if raw_dt is not None and (not isinstance(raw_dt, str) or not raw_dt.strip()):
        return jsonify({"error": "meeting_datetime must be ISO 8601 string or null"}), 400

    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            return jsonify({"error": "file_not_found"}), 404
        session_obj = db.query(UploadSession).filter(UploadSession.id == file_obj.session_id).first()
        if not session_obj:
            return jsonify({"error": "session_not_found"}), 404
        try:
            data = request_internal_device_api(
                "POST", "/api/v1/files/by-session/meeting-datetime",
                json_body={
                    "user_sub": user["sub"],
                    "simple_code": session_obj.simple_code,
                    "original_filename": file_obj.original_filename,
                    "meeting_datetime": raw_dt.strip() if isinstance(raw_dt, str) else None,
                },
            )
        except req.HTTPError as err:
            if err.response is not None:
                try:
                    body = err.response.json()
                except Exception:
                    body = {"error": "internal_api_error"}
                return jsonify({"error": body.get("error", "meeting_datetime_failed")}), err.response.status_code
            return jsonify({"error": "meeting_datetime_failed"}), 502
        return jsonify({
            "ok": True,
            "meeting_datetime": data.get("meeting_datetime"),
            "meeting_datetime_overridden": data.get("meeting_datetime") is not None,
        })
    finally:
        db.close()


# ─── Local upload (sans QR) ─────────────────────────────────────────────


def _get_or_create_local_upload_session(db, user):
    sess = (
        db.query(UploadSession)
        .filter(
            UploadSession.user_sub == user["sub"],
            UploadSession.simple_code.like(f"{svc.LOCAL_UPLOAD_SIMPLE_CODE_PREFIX}%"),
            UploadSession.status == SessionStatus.ACTIVE,
            UploadSession.trashed_at.is_(None),
            UploadSession.upload_count < UploadSession.max_uploads,
        )
        .order_by(UploadSession.created_at.desc())
        .first()
    )
    if sess:
        return sess
    suffix = secrets.token_hex(4).upper()
    simple_code = f"{svc.LOCAL_UPLOAD_SIMPLE_CODE_PREFIX}{suffix}"
    new = UploadSession(
        id=uuid4(),
        user_sub=user.get("sub"),
        user_email=user.get("email"),
        user_display_name=user.get("name") or user.get("preferred_username"),
        simple_code=simple_code,
        qr_token=secrets.token_hex(32),
        status=SessionStatus.ACTIVE,
        max_uploads=svc.LOCAL_UPLOAD_MAX_PER_SESSION,
        upload_count=0,
        ttl_minutes=0,
        expires_at=datetime.now(timezone.utc) + timedelta(days=365 * 50),
    )
    db.add(new)
    db.flush()
    return new


@bp.route("/api/my-upload", methods=["POST"])
@require_auth
def api_my_upload():
    user = get_current_user()
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier sélectionné."}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Nom de fichier vide."}), 400
    if not is_allowed_audio_filename(file.filename):
        return jsonify({
            "error": f"Format non supporté. Formats acceptés : {', '.join(ALLOWED_AUDIO_EXTENSIONS)}"
        }), 400

    file_data = file.read()
    file_size = len(file_data)
    if file_size == 0:
        return jsonify({"error": "Fichier vide."}), 400

    s3_upload_cfg = get_s3_upload_cfg()
    db = session_scope()
    try:
        session_obj = _get_or_create_local_upload_session(db, user)
        stored_name = build_stored_filename(session_obj.simple_code, file.filename)
        try:
            store_audio_to_s3(s3_upload_cfg, stored_name, file_data, file.content_type)
        except Exception:
            db.rollback()
            logger.exception("S3 upload failed for local upload")
            return jsonify({"error": "Erreur lors de l'upload. Réessayez."}), 500

        uploaded_file = UploadedFile(
            id=uuid4(),
            session_id=session_obj.id,
            original_filename=file.filename,
            stored_filename=stored_name,
            file_size_bytes=file_size,
            mime_type=file.content_type,
            status=UploadStatus.PENDING,
            status_message="Fichier reçu (upload local), en attente d'analyse antivirale...",
        )
        db.add(uploaded_file)
        session_obj.upload_count += 1
        db.commit()
        file_id = str(uploaded_file.id)
        simple_code = session_obj.simple_code
        user_sub = session_obj.user_sub
        user_email = session_obj.user_email
        session_id_str = str(session_obj.id)
        remaining = max(0, session_obj.max_uploads - session_obj.upload_count)
    finally:
        db.close()

    try:
        publish_av_scan_message(
            get_rabbit_cfg(),
            file_id=file_id, session_id=session_id_str,
            stored_filename=stored_name, original_filename=file.filename,
            simple_code=simple_code, user_sub=user_sub, user_email=user_email,
        )
    except Exception:
        logger.exception("Failed to publish to QUEUE_AV_SCAN for local upload")

    return jsonify({
        "file_id": file_id, "filename": file.filename,
        "status": "pending", "remaining": remaining,
    })


# ─── Trash / soft-delete ────────────────────────────────────────────────


@bp.route("/api/my-trash", methods=["GET"])
@require_auth
def api_my_trash():
    user = get_current_user()
    db = session_scope()
    try:
        try:
            svc.purge_expired_trash(db, user["sub"])
            db.commit()
        except Exception:
            db.rollback()

        now = datetime.now(timezone.utc)
        files_q = (
            db.query(UploadedFile)
            .join(UploadSession, UploadSession.id == UploadedFile.session_id)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.is_(None),
                UploadedFile.trashed_at.isnot(None),
            )
            .order_by(UploadedFile.trashed_at.desc())
            .all()
        )
        files_list = [{
            "id": str(f.id),
            "original_filename": f.original_filename,
            "simple_code": f.session.simple_code if f.session else None,
            "trashed_at": f.trashed_at.isoformat() if f.trashed_at else None,
            "days_left": max(0, svc.TRASH_RETENTION_DAYS - (now - f.trashed_at.replace(tzinfo=timezone.utc)).days)
                if f.trashed_at else None,
        } for f in files_q]

        sessions_q = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.isnot(None),
            )
            .order_by(UploadSession.trashed_at.desc())
            .all()
        )
        sessions_list = [{
            "simple_code": s.simple_code,
            "id": str(s.id),
            "trashed_at": s.trashed_at.isoformat() if s.trashed_at else None,
            "days_left": max(0, svc.TRASH_RETENTION_DAYS - (now - s.trashed_at.replace(tzinfo=timezone.utc)).days)
                if s.trashed_at else None,
            "files_count": len(s.uploads),
        } for s in sessions_q]

        def _enrich_with_days_left(items, title_keys):
            out = []
            for it in items:
                trashed_iso = it.get("trashed_at")
                days_left = None
                if trashed_iso:
                    try:
                        ts = datetime.fromisoformat(trashed_iso.replace("Z", "+00:00"))
                        days_left = max(0, svc.TRASH_RETENTION_DAYS - (now - ts.astimezone(timezone.utc)).days)
                    except Exception:
                        days_left = None
                title = None
                for k in title_keys:
                    title = it.get(k)
                    if title:
                        break
                out.append({
                    "id": it.get("id"),
                    "title": title or "(sans titre)",
                    "trashed_at": trashed_iso,
                    "days_left": days_left,
                })
            return out

        preparations_list = []
        try:
            data = request_internal_device_api(
                "GET", "/api/v1/preparations",
                params={"user_sub": user["sub"], "trashed": "true", "limit": 200},
            )
            preparations_list = _enrich_with_days_left(
                data.get("preparations", []), ("title", "subject"),
            )
        except Exception:
            logger.debug("trash listing: preparation relay failed", exc_info=True)

        meetings_list = []
        try:
            data = request_internal_device_api(
                "GET", "/api/v1/meetings",
                params={"user_sub": user["sub"], "trashed": "true", "limit": 200},
            )
            meetings_list = _enrich_with_days_left(
                data.get("meetings", []), ("title", "summary"),
            )
        except Exception:
            logger.debug("trash listing: meeting relay failed", exc_info=True)

        return jsonify({
            "files": files_list,
            "sessions": sessions_list,
            "preparations": preparations_list,
            "meetings": meetings_list,
            "retention_days": svc.TRASH_RETENTION_DAYS,
        })
    finally:
        db.close()


@bp.route("/api/file/<file_id>/restore", methods=["POST"])
@require_auth
def api_restore_file(file_id):
    user = get_current_user()
    db = session_scope()
    try:
        f = (
            db.query(UploadedFile)
            .join(UploadSession, UploadSession.id == UploadedFile.session_id)
            .filter(
                UploadedFile.id == file_id,
                UploadSession.user_sub == user["sub"],
                UploadedFile.trashed_at.isnot(None),
            )
            .first()
        )
        if not f:
            return jsonify({"error": "file_not_in_trash"}), 404
        f.trashed_at = None
        db.commit()
        return jsonify({"ok": True, "restored": True, "filename": f.original_filename})
    except Exception:
        db.rollback()
        logger.exception("Failed to restore file %s", file_id)
        return jsonify({"error": "restore_failed"}), 500
    finally:
        db.close()


@bp.route("/api/my-sessions/<simple_code>/restore", methods=["POST"])
@require_auth
def api_restore_session(simple_code):
    user = get_current_user()
    db = session_scope()
    try:
        s = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.simple_code == simple_code,
                UploadSession.trashed_at.isnot(None),
            )
            .first()
        )
        if not s:
            return jsonify({"error": "session_not_in_trash"}), 404
        s.trashed_at = None
        db.commit()
        return jsonify({"ok": True, "restored": True, "simple_code": simple_code})
    except Exception:
        db.rollback()
        logger.exception("Failed to restore session %s", simple_code)
        return jsonify({"error": "restore_failed"}), 500
    finally:
        db.close()


@bp.route("/api/file/<file_id>/permanently", methods=["DELETE"])
@require_auth
def api_delete_file_permanently(file_id):
    user = get_current_user()
    s3_upload_cfg = get_s3_upload_cfg()
    s3_processed_cfg = get_s3_processed_cfg()
    db = session_scope()
    deleted_objects = 0
    simple_code = None
    original_filename = None
    try:
        f = (
            db.query(UploadedFile)
            .join(UploadSession, UploadSession.id == UploadedFile.session_id)
            .filter(
                UploadedFile.id == file_id,
                UploadSession.user_sub == user["sub"],
                UploadedFile.trashed_at.isnot(None),
            )
            .first()
        )
        if not f:
            return jsonify({"error": "file_not_in_trash"}), 404
        session_obj = db.query(UploadSession).filter(UploadSession.id == f.session_id).first()
        simple_code = session_obj.simple_code if session_obj else None
        original_filename = f.original_filename
        for cfg, key in (
            (s3_upload_cfg, f.stored_filename),
            (s3_processed_cfg, f.transcoded_filename),
        ):
            try:
                if key:
                    delete_object(cfg, key)
                    deleted_objects += 1
            except Exception:
                logger.warning("Permanent delete: S3 fail %s", key, exc_info=True)
        try:
            t_cfg, t_key = svc.resolve_transferred_storage(db, f)
            if t_cfg and t_key:
                delete_object(t_cfg, t_key)
                deleted_objects += 1
        except Exception:
            logger.warning("Permanent delete: transferred fail", exc_info=True)
        db.delete(f)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Permanent delete file %s failed", file_id)
        return jsonify({"error": "delete_failed"}), 500
    finally:
        db.close()

    if simple_code and original_filename:
        try:
            request_internal_device_api(
                "DELETE", "/api/v1/files/by-session",
                json_body={
                    "user_sub": user.get("sub", ""),
                    "simple_code": simple_code,
                    "original_filename": original_filename,
                },
            )
        except Exception:
            logger.debug("Internal cleanup post-purge failed for %s", file_id, exc_info=True)

    return jsonify({"ok": True, "deleted_objects": deleted_objects})


@bp.route("/api/file/<file_id>", methods=["DELETE"])
@require_auth
def api_delete_file(file_id):
    user = get_current_user()
    db = session_scope()
    original_filename = None
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            return jsonify({"error": "file_not_found"}), 404
        file_obj.trashed_at = datetime.now(timezone.utc)
        original_filename = file_obj.original_filename
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to trash file %s for user %s", file_id, user.get("sub"))
        return jsonify({"error": "file_delete_failed"}), 500
    finally:
        db.close()

    return jsonify({
        "ok": True, "trashed": True,
        "retention_days": svc.TRASH_RETENTION_DAYS,
        "filename": original_filename,
    })


@bp.route("/api/my-sessions/<simple_code>", methods=["DELETE"])
@require_auth
def api_delete_session(simple_code):
    user = get_current_user()
    db = session_scope()
    deleted_files = 0
    try:
        s = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.simple_code == simple_code,
                UploadSession.trashed_at.is_(None),
            )
            .first()
        )
        if not s:
            return jsonify({"error": "session_not_found"}), 404
        s.trashed_at = datetime.now(timezone.utc)
        deleted_files = sum(1 for _ in s.uploads)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to trash session %s for user %s", simple_code, user.get("sub"))
        return jsonify({"error": "session_delete_failed"}), 500
    finally:
        db.close()

    return jsonify({
        "ok": True, "trashed": True,
        "retention_days": svc.TRASH_RETENTION_DAYS,
        "deleted_files": deleted_files,
    })


@bp.route("/api/purge-my-sessions", methods=["POST"])
@require_auth
def api_purge_my_sessions():
    user = get_current_user()
    db = session_scope()
    deleted_sessions = 0
    deleted_files = 0
    try:
        now = datetime.now(timezone.utc)
        sessions = (
            db.query(UploadSession)
            .filter(
                UploadSession.user_sub == user["sub"],
                UploadSession.trashed_at.is_(None),
            )
            .all()
        )
        for s in sessions:
            s.trashed_at = now
            deleted_files += sum(1 for _ in s.uploads)
            deleted_sessions += 1
        db.commit()
        return jsonify({
            "ok": True, "trashed": True,
            "retention_days": svc.TRASH_RETENTION_DAYS,
            "deleted_sessions": deleted_sessions,
            "deleted_files": deleted_files,
        })
    except Exception:
        db.rollback()
        logger.exception("Failed to trash user sessions for %s", user["sub"])
        return jsonify({"error": "Failed to purge sessions"}), 500
    finally:
        db.close()


# ─── Normalization impact ───────────────────────────────────────────────


@bp.route("/api/file/normalization-impact/<file_id>")
@require_auth
def api_file_normalization_impact(file_id):
    user = get_current_user()
    s3_upload_cfg = get_s3_upload_cfg()
    s3_processed_cfg = get_s3_processed_cfg()
    db = session_scope()
    try:
        file_obj = svc.get_owned_file(db, user["sub"], file_id)
        if not file_obj:
            abort(404, "File not found")
        if not file_obj.transcoded_filename:
            return jsonify({"error": "Fichier pas encore transcodé"}), 400

        if (file_obj.normalization_source_i is not None
                and file_obj.normalization_output_i is not None):
            source = {
                "i":   file_obj.normalization_source_i,
                "tp":  file_obj.normalization_source_tp,
                "lra": file_obj.normalization_source_lra,
            }
            normalized = {
                "i":   file_obj.normalization_output_i,
                "tp":  file_obj.normalization_output_tp,
                "lra": file_obj.normalization_output_lra,
            }
            target_i = -16.0
            source_dist = abs(source["i"] - target_i)
            normalized_dist = abs(normalized["i"] - target_i)
            improvement = round(source_dist - normalized_dist, 2)
            return jsonify({
                "target": {"i": target_i, "tp": -1.5, "lra": 11.0},
                "source": source, "normalized": normalized,
                "delta": {
                    "i":   round(normalized["i"] - source["i"], 2),
                    "tp":  round(normalized["tp"] - source["tp"], 2),
                    "lra": round(normalized["lra"] - source["lra"], 2),
                },
                "improvement_to_target_lufs": improvement,
                "from_cache": True,
            })

        try:
            if not object_exists(s3_upload_cfg, file_obj.stored_filename or ""):
                return jsonify({
                    "error": "Source audio purgée (la mesure n'était pas "
                             "persistée pour ce fichier ; les nouveaux uploads "
                             "auront les valeurs disponibles directement)."
                }), 410
        except Exception:
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            src_suffix = Path(file_obj.stored_filename or "").suffix or ".audio"
            out_suffix = Path(file_obj.transcoded_filename or "").suffix or ".wav"
            src_path = os.path.join(tmpdir, f"source{src_suffix}")
            out_path = os.path.join(tmpdir, f"normalized{out_suffix}")
            with open(src_path, "wb") as src_f:
                src_f.write(download_fileobj(s3_upload_cfg, file_obj.stored_filename).read())
            with open(out_path, "wb") as out_f:
                out_f.write(download_fileobj(s3_processed_cfg, file_obj.transcoded_filename).read())
            source = svc.run_loudnorm_measure(src_path)
            normalized = svc.run_loudnorm_measure(out_path)

        target_i = -16.0
        source_dist = abs(source["i"] - target_i)
        normalized_dist = abs(normalized["i"] - target_i)
        improvement = round(source_dist - normalized_dist, 2)
        return jsonify({
            "target": {"i": target_i, "tp": -1.5, "lra": 11.0},
            "source": source, "normalized": normalized,
            "delta": {
                "i": round(normalized["i"] - source["i"], 2),
                "tp": round(normalized["tp"] - source["tp"], 2),
                "lra": round(normalized["lra"] - source["lra"], 2),
            },
            "improvement_to_target_lufs": improvement,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout analyse audio"}), 504
    except Exception:
        logger.exception("Failed to compute normalization impact for file %s", file_id)
        return jsonify({"error": "Analyse indisponible"}), 500
    finally:
        db.close()
