"""Blueprint ``devices`` — QR / enroll / retention / my-devices.

PR3-v2 : extraction depuis ``main.py``. URLs canoniques préservées
pour le front et l'mobile-upload-pwa.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urlencode
from uuid import uuid4

import qrcode
import requests as req
from flask import Blueprint, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
from libs.shared.app.config import (  # noqa: E402
    CODE_TTL_MAX_MINUTES, CODE_TTL_MINUTES, INTERNAL_API_TOKEN,
    MAX_UPLOADS_PER_SESSION, TOKEN_ISSUER_API_URL, UPLOAD_PORTAL_BASE_URL,
    UPLOAD_STATUS_VIEW_TTL_MINUTES,
)
from libs.shared.app.models import SessionStatus, UploadSession, UploadTokenOption  # noqa: E402
from libs.shared.app.security import verify_bearer_token, resolve_auto_transcribe  # noqa: E402

from app.runtime import (
    allow_short_qr_ttl, get_public_host, session_scope,
)
from app.shared import (
    get_current_user, require_auth, request_internal_device_api,
)

logger = logging.getLogger("mesreunions_web.devices.routes")

bp = Blueprint("devices", __name__)


# ── Helpers ─────────────────────────────────────────────────────────────


def make_qr_image(url: str) -> BytesIO:
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _mobile_upload_pwa_base_url() -> str:
    public_host = get_public_host()
    if public_host:
        return f"{request.scheme}://{public_host}:8081"
    configured = (UPLOAD_PORTAL_BASE_URL or "").strip().rstrip("/")
    lowered = configured.lower()
    if configured and ("localhost" not in lowered and "127.0.0.1" not in lowered):
        return configured
    forwarded_host = (request.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
    if forwarded_host:
        host = forwarded_host.split(":", 1)[0].strip("[]")
    else:
        host = request.host.split(":", 1)[0].strip("[]")
    if host and host not in {"localhost", "127.0.0.1"}:
        return f"{request.scheme}://{host}:8081"
    return configured or "http://localhost:8081"


def _request_token_from_internal(user, ttl_minutes, max_uploads,
                                  ttl_seconds=None, auto_transcribe=True):
    payload = {
        "user_sub": user["sub"],
        "user_email": user.get("email"),
        "user_display_name": user.get("name"),
        "ttl_minutes": ttl_minutes,
        "max_uploads": max_uploads,
        "auto_transcribe": bool(auto_transcribe),
    }
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds
    resp = req.post(
        TOKEN_ISSUER_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# ── Routes ──────────────────────────────────────────────────────────────


@bp.route("/api/generate-code", methods=["POST"])
@require_auth
def api_generate_code():
    user = get_current_user()
    data = request.get_json(silent=True) or {}

    ttl_raw = str(data.get("ttl_minutes", CODE_TTL_MINUTES))
    ttl_seconds = None
    if ttl_raw.endswith("s"):
        if not allow_short_qr_ttl():
            return jsonify({"error": "Short TTL test mode is disabled"}), 400
        try:
            ttl_seconds = int(ttl_raw[:-1])
        except ValueError:
            return jsonify({"error": "Invalid short TTL value"}), 400
        if ttl_seconds not in {15, 30}:
            return jsonify({"error": "Allowed short TTL values: 15s, 30s"}), 400
        ttl_minutes = 1
    else:
        ttl_minutes = min(max(int(ttl_raw), 1), CODE_TTL_MAX_MINUTES)
    # Cap au plafond serveur (MAX_UPLOADS_PER_SESSION = 299 par défaut,
    # surchargeable via env). L'ancien cap dur à 50 était un reliquat
    # d'une phase de tests qui plafonnait silencieusement les QR à 50
    # fichiers même si le client demandait 299 — cf bug PU7S39 2026-05-21.
    max_uploads = min(max(int(data.get("max_uploads", MAX_UPLOADS_PER_SESSION)), 1),
                      MAX_UPLOADS_PER_SESSION)
    # Politique serveur (défaut OFF) : la valeur demandée par l'utilisateur
    # n'est honorée que si AUTO_TRANSCRIBE_POLICY l'autorise. Cohérent avec
    # le garde-fou de l'autorité interne (device-token-authority).
    auto_transcribe = resolve_auto_transcribe(data.get("auto_transcribe", False))

    try:
        token_data = _request_token_from_internal(
            user, ttl_minutes, max_uploads,
            ttl_seconds=ttl_seconds, auto_transcribe=auto_transcribe,
        )
    except req.RequestException:
        logger.exception("Failed to request token from internal zone")
        return jsonify({"error": "Service de génération de token indisponible. Réessayez."}), 503

    simple_code = token_data["simple_code"]
    qr_token = token_data["qr_token"]
    expires_at = datetime.fromisoformat(token_data["expires_at"])

    upload_session = UploadSession(
        id=uuid4(),
        user_sub=user["sub"],
        user_email=user.get("email"),
        user_display_name=user.get("name"),
        simple_code=simple_code,
        qr_token=qr_token,
        max_uploads=max_uploads,
        ttl_minutes=ttl_minutes,
        expires_at=expires_at,
        status_view_expires_at=expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES),
    )

    db = session_scope()
    try:
        db.add(upload_session)
        db.add(UploadTokenOption(
            id=uuid4(), qr_token=qr_token, simple_code=simple_code,
            auto_transcribe=auto_transcribe,
        ))
        db.commit()
    finally:
        db.close()

    upload_url = f"{_mobile_upload_pwa_base_url()}/upload/{qr_token}"

    return jsonify({
        "session_id": str(upload_session.id),
        "simple_code": simple_code, "qr_token": qr_token,
        "upload_url": upload_url,
        "expires_at": expires_at.isoformat(),
        "ttl_minutes": ttl_minutes,
        "ttl_seconds": token_data.get("ttl_seconds"),
        "max_uploads": max_uploads,
        "auto_transcribe": auto_transcribe,
    })


@bp.route("/api/qr-image/<qr_token>")
@require_auth
def api_qr_image(qr_token):
    upload_url = f"{_mobile_upload_pwa_base_url()}/upload/{qr_token}"
    buf = make_qr_image(upload_url)
    return buf.getvalue(), 200, {"Content-Type": "image/png"}


@bp.route("/api/my-devices")
@require_auth
def api_my_devices():
    user = get_current_user()
    db = session_scope()
    try:
        devices = request_internal_device_api(
            "GET", "/api/v1/devices",
            params={"user_sub": user.get("sub", "")},
        )
        devices = devices if isinstance(devices, list) else []

        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)
        sessions = (
            db.query(UploadSession)
            .filter(UploadSession.user_sub == user.get("sub", ""))
            .all()
        )
        session_by_qr = {}
        for s in sessions:
            token = (s.qr_token or "").strip()
            if not token:
                continue
            prev = session_by_qr.get(token)
            if prev is None or (s.created_at and prev.created_at and s.created_at > prev.created_at):
                session_by_qr[token] = s

        enriched = []
        for d in devices:
            item = dict(d) if isinstance(d, dict) else {}
            qr_token = (item.get("qr_token") or "").strip()
            s = session_by_qr.get(qr_token)
            if s is not None:
                recent_uploads_24h = 0
                for f in (s.uploads or []):
                    created = f.created_at
                    if created is None:
                        continue
                    created_utc = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                    if created_utc >= day_ago:
                        recent_uploads_24h += 1
                remaining_uploads = max(0, int(s.max_uploads or 0) - int(s.upload_count or 0))
                session_max_uploads = int(s.max_uploads or 0)
                session_upload_count = int(s.upload_count or 0)
                expires_at = s.expires_at
                expiring_soon = False
                if expires_at:
                    exp_utc = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
                    expiring_soon = exp_utc <= (now + timedelta(days=7))
                item["recent_uploads_24h"] = recent_uploads_24h
                item["remaining_uploads"] = remaining_uploads
                item["session_max_uploads"] = session_max_uploads
                item["session_upload_count"] = session_upload_count
                item["session_simple_code"] = s.simple_code
                item["session_expiring_soon"] = expiring_soon
                item["session_expires_at"] = expires_at.isoformat() if expires_at else None
            else:
                item["recent_uploads_24h"] = 0
                item["remaining_uploads"] = 0
                item["session_max_uploads"] = 0
                item["session_upload_count"] = 0
                item["session_simple_code"] = None
                item["session_expiring_soon"] = False
                item["session_expires_at"] = None
            enriched.append(item)

        return jsonify(enriched)
    except Exception:
        logger.exception("Failed to list enrolled devices for user %s", user.get("sub"))
        return jsonify({"error": "device_list_unavailable"}), 503
    finally:
        db.close()


@bp.route("/api/my-devices/<device_id>/rename", methods=["POST"])
@require_auth
def api_rename_device(device_id):
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    name = (data.get("device_name") or "").strip()
    if not name:
        return jsonify({"error": "device_name requis"}), 400
    try:
        request_internal_device_api(
            "POST", f"/api/v1/devices/{device_id}/rename",
            json_body={"user_sub": user.get("sub", ""), "device_name": name},
        )
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to rename device %s for user %s", device_id, user.get("sub"))
        return jsonify({"error": "device_rename_failed"}), 500


@bp.route("/api/my-devices/<device_id>/revoke", methods=["POST"])
@require_auth
def api_revoke_device(device_id):
    user = get_current_user()
    try:
        request_internal_device_api(
            "POST", f"/api/v1/devices/{device_id}/revoke",
            json_body={"user_sub": user.get("sub", ""), "reason": "revoked_from_qr_ui"},
        )
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to revoke device %s for user %s", device_id, user.get("sub"))
        return jsonify({"error": "device_revoke_failed"}), 500


@bp.route("/api/my-devices/<device_id>", methods=["DELETE"])
@require_auth
def api_delete_device(device_id):
    user = get_current_user()
    try:
        request_internal_device_api(
            "DELETE", f"/api/v1/devices/{device_id}",
            json_body={"user_sub": user.get("sub", "")},
        )
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to delete device %s for user %s", device_id, user.get("sub"))
        return jsonify({"error": "device_delete_failed"}), 500


@bp.route("/api/my-devices/revoke-all", methods=["POST"])
@require_auth
def api_revoke_all_devices():
    user = get_current_user()
    try:
        data = request_internal_device_api(
            "POST", "/api/v1/devices/revoke-all",
            json_body={"user_sub": user.get("sub", ""), "reason": "revoked_all_from_qr_ui"},
        )
        return jsonify({"ok": True, "revoked": int(data.get("revoked", 0))})
    except Exception:
        logger.exception("Failed to revoke all devices for user %s", user.get("sub"))
        return jsonify({"error": "device_revoke_all_failed"}), 500


@bp.route("/api/my-token/renew-7d", methods=["POST"])
@require_auth
def api_renew_token_7d():
    user = get_current_user()
    payload = request.get_json(silent=True) or {}
    qr_token = (payload.get("qr_token") or "").strip()
    if not qr_token:
        return jsonify({"error": "qr_token_required"}), 400
    ttl_raw = str(payload.get("ttl_minutes", "")).strip()
    ttl_minutes = None
    ttl_seconds = None
    if ttl_raw:
        if ttl_raw.endswith("s"):
            if not allow_short_qr_ttl():
                return jsonify({"error": "Short TTL test mode is disabled"}), 400
            try:
                ttl_seconds = int(ttl_raw[:-1])
            except ValueError:
                return jsonify({"error": "Invalid ttl format"}), 400
            if ttl_seconds not in {15, 30}:
                return jsonify({"error": "Allowed ttl_seconds values are 15 or 30"}), 400
            ttl_minutes = 1
        else:
            try:
                ttl_minutes = min(max(int(ttl_raw), 1), CODE_TTL_MAX_MINUTES)
            except ValueError:
                return jsonify({"error": "Invalid ttl format"}), 400
    add_uploads = min(max(int(payload.get("add_uploads", 0) or 0), 0), 500)

    db = session_scope()
    try:
        data = request_internal_device_api(
            "POST", "/api/v1/tokens/extend-7d",
            json_body={
                "user_sub": user["sub"], "qr_token": qr_token,
                "ttl_minutes": ttl_minutes, "ttl_seconds": ttl_seconds,
                "add_uploads": add_uploads,
            },
        )
        new_expires_raw = data.get("expires_at")
        if not new_expires_raw:
            raise ValueError("missing expires_at from internal API")
        new_expires_at = datetime.fromisoformat(str(new_expires_raw))
        if new_expires_at.tzinfo is None:
            new_expires_at = new_expires_at.replace(tzinfo=timezone.utc)
        new_status_view_raw = data.get("status_view_expires_at")
        if new_status_view_raw:
            new_status_view = datetime.fromisoformat(str(new_status_view_raw))
            if new_status_view.tzinfo is None:
                new_status_view = new_status_view.replace(tzinfo=timezone.utc)
        else:
            new_status_view = new_expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES)

        session_obj = (
            db.query(UploadSession)
            .filter(UploadSession.user_sub == user["sub"], UploadSession.qr_token == qr_token)
            .first()
        )
        if session_obj:
            session_obj.expires_at = new_expires_at
            session_obj.status_view_expires_at = new_status_view
            session_obj.status = SessionStatus.ACTIVE
            if add_uploads > 0:
                session_obj.max_uploads = min(1000, int(session_obj.max_uploads or 0) + add_uploads)
            db.commit()
        else:
            db.rollback()

        return jsonify({
            "ok": True,
            "expires_at": new_expires_at.isoformat(),
            "renew_days": int(data.get("renew_days", 7)),
            "max_uploads": int(data.get("max_uploads", 0) or 0),
        })
    except req.HTTPError as err:
        db.rollback()
        if err.response is not None:
            try:
                body = err.response.json()
            except Exception:
                body = {"error": "internal_api_error"}
            return jsonify({"error": body.get("error", "renew_failed")}), err.response.status_code
        return jsonify({"error": "renew_failed"}), 502
    except Exception:
        db.rollback()
        logger.exception("Failed to renew token for user %s", user.get("sub"))
        return jsonify({"error": "renew_failed"}), 500
    finally:
        db.close()


@bp.route("/api/my-sessions/<session_id>/renew-7d", methods=["POST"])
@require_auth
def api_renew_session_7d(session_id):
    user = get_current_user()
    db = session_scope()
    try:
        session_obj = (
            db.query(UploadSession)
            .filter(UploadSession.id == session_id, UploadSession.user_sub == user["sub"])
            .first()
        )
        if not session_obj:
            return jsonify({"error": "session_not_found"}), 404

        payload = request.get_json(silent=True) or {}
        ttl_raw = str(payload.get("ttl_minutes", "")).strip()
        ttl_minutes = None
        ttl_seconds = None
        if ttl_raw:
            if ttl_raw.endswith("s"):
                if not allow_short_qr_ttl():
                    return jsonify({"error": "Short TTL test mode is disabled"}), 400
                try:
                    ttl_seconds = int(ttl_raw[:-1])
                except ValueError:
                    return jsonify({"error": "Invalid ttl format"}), 400
                if ttl_seconds not in {15, 30}:
                    return jsonify({"error": "Allowed ttl_seconds values are 15 or 30"}), 400
                ttl_minutes = 1
            else:
                try:
                    ttl_minutes = min(max(int(ttl_raw), 1), CODE_TTL_MAX_MINUTES)
                except ValueError:
                    return jsonify({"error": "Invalid ttl format"}), 400
        add_uploads = min(max(int(payload.get("add_uploads", 0) or 0), 0), 500)

        data = request_internal_device_api(
            "POST", "/api/v1/tokens/extend-7d",
            json_body={
                "user_sub": user["sub"], "qr_token": session_obj.qr_token,
                "ttl_minutes": ttl_minutes, "ttl_seconds": ttl_seconds,
                "add_uploads": add_uploads,
            },
        )
        new_expires_raw = data.get("expires_at")
        if not new_expires_raw:
            raise ValueError("missing expires_at from internal API")
        new_expires_at = datetime.fromisoformat(str(new_expires_raw))
        if new_expires_at.tzinfo is None:
            new_expires_at = new_expires_at.replace(tzinfo=timezone.utc)
        new_status_view_raw = data.get("status_view_expires_at")
        if new_status_view_raw:
            new_status_view = datetime.fromisoformat(str(new_status_view_raw))
            if new_status_view.tzinfo is None:
                new_status_view = new_status_view.replace(tzinfo=timezone.utc)
        else:
            new_status_view = new_expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES)

        session_obj.expires_at = new_expires_at
        session_obj.status_view_expires_at = new_status_view
        session_obj.status = SessionStatus.ACTIVE
        if add_uploads > 0:
            session_obj.max_uploads = min(1000, int(session_obj.max_uploads or 0) + add_uploads)
        db.commit()

        return jsonify({
            "ok": True, "session_id": str(session_obj.id),
            "expires_at": session_obj.expires_at.isoformat(),
            "renew_days": int(data.get("renew_days", 7)),
            "max_uploads": int(data.get("max_uploads", 0) or 0),
        })
    except req.HTTPError as err:
        db.rollback()
        if err.response is not None:
            try:
                body = err.response.json()
            except Exception:
                body = {"error": "internal_api_error"}
            return jsonify({"error": body.get("error", "renew_failed")}), err.response.status_code
        return jsonify({"error": "renew_failed"}), 502
    except Exception:
        db.rollback()
        logger.exception("Failed to renew session %s for user %s", session_id, user.get("sub"))
        return jsonify({"error": "renew_failed"}), 500
    finally:
        db.close()


@bp.route("/api/device/enroll-proxy", methods=["POST"])
def api_device_enroll_proxy():
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        data = request_internal_device_api("POST", "/api/v1/enroll-device", json_body=payload)
        return jsonify(data)
    except req.HTTPError as err:
        if err.response is not None:
            try:
                return jsonify(err.response.json()), err.response.status_code
            except Exception:
                return jsonify({"error": "device_enroll_proxy_failed"}), err.response.status_code
        return jsonify({"error": "device_enroll_proxy_failed"}), 502
    except Exception:
        logger.exception("Device enroll proxy failed")
        return jsonify({"error": "device_enroll_proxy_failed"}), 502


@bp.route("/api/device/validate-proxy", methods=["POST"])
def api_device_validate_proxy():
    auth = request.headers.get("Authorization", "")
    if not verify_bearer_token(auth, INTERNAL_API_TOKEN):
        return jsonify({"error": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        data = request_internal_device_api("POST", "/api/v1/validate-device", json_body=payload)
        return jsonify(data)
    except req.HTTPError as err:
        if err.response is not None:
            try:
                return jsonify(err.response.json()), err.response.status_code
            except Exception:
                return jsonify({"valid": False, "reason": "upstream_error"}), 502
        return jsonify({"valid": False, "reason": "upstream_error"}), 502
    except Exception:
        logger.exception("Device validate proxy failed")
        return jsonify({"valid": False, "reason": "proxy_error"}), 502
