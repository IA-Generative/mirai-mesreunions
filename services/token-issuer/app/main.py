"""
Token Issuer Service (Zone Interne)
====================================
Seule autorité de génération des tokens de session (simple_code + qr_token).
Le code-generator (zone externe) appelle cette API pour obtenir un token.
La zone interne est ainsi maître des identifiants de liaison.

FLUX :
  1. Code-generator (ext) → POST /api/v1/issue-token {user_sub, ttl, max_uploads}
  2. Token-issuer (int)   → génère simple_code + qr_token, stocke en base interne
  3. Token-issuer (int)   → renvoie {simple_code, qr_token, expires_at}
  4. Code-generator (ext) → stocke la copie en base externe, affiche QR

SÉCURITÉ :
  - Authentifié par bearer token (INTERNAL_API_TOKEN)
  - Le token cryptographique (qr_token) est généré en zone sûre
  - La zone externe ne fait que relayer, elle ne peut pas forger de token
"""

import logging
import math
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from flask import Flask, request, jsonify

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    load_int_db, INTERNAL_API_TOKEN,
    CODE_TTL_MINUTES, CODE_TTL_MAX_MINUTES, MAX_UPLOADS_PER_SESSION, CODE_LENGTH,
    UPLOAD_STATUS_VIEW_TTL_MINUTES,
)
from libs.shared.app.models import InternalBase, IssuedToken, DeviceEnrollment
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.device_token import create_device_token, verify_device_token, utc_now_ts

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

db_cfg = load_int_db()
SessionLocal = None
ALLOW_SHORT_QR_TTL_SECONDS_TEST = os.getenv("ALLOW_SHORT_QR_TTL_SECONDS_TEST", "").lower() in {"1", "true", "yes"}
DEVICE_TOKEN_RETENTION_HOURS = max(1, int(os.getenv("DEVICE_TOKEN_RETENTION_HOURS", "168")))


# ─── Helpers ────────────────────────────────────────────────

def generate_simple_code(length: int = CODE_LENGTH) -> str:
    """Code lisible humain : majuscules + chiffres, sans ambiguïté (pas de 0/O, 1/I/L)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_qr_token() -> str:
    """Token cryptographique sûr pour les URL QR."""
    return secrets.token_urlsafe(32)


def verify_token():
    auth = request.headers.get("Authorization", "")
    return verify_bearer_token(auth, INTERNAL_API_TOKEN)


# ─── Routes ─────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "token-issuer", "zone": "internal"})


@app.route("/api/v1/issue-token", methods=["POST"])
def issue_token():
    """
    Génère un couple (simple_code, qr_token) et l'enregistre en base interne.
    Appelé par le code-generator (zone externe) via API authentifiée.
    """
    if not verify_token():
        logger.warning("Unauthorized token issue request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "Missing JSON body"}), 400

    user_sub = data.get("user_sub")
    if not user_sub:
        return jsonify({"error": "Missing user_sub"}), 400

    now = datetime.now(timezone.utc)

    ttl_seconds = data.get("ttl_seconds")
    if ttl_seconds is not None:
        try:
            ttl_seconds = int(ttl_seconds)
        except (TypeError, ValueError):
            return jsonify({"error": "ttl_seconds must be an integer"}), 400
        if not ALLOW_SHORT_QR_TTL_SECONDS_TEST:
            return jsonify({"error": "Short TTL test mode is disabled"}), 400
        if ttl_seconds not in {15, 30}:
            return jsonify({"error": "Allowed ttl_seconds values are 15 or 30"}), 400
        ttl_minutes = max(1, math.ceil(ttl_seconds / 60))
        expires_at = now + timedelta(seconds=ttl_seconds)
    else:
        ttl_minutes = min(
            max(int(data.get("ttl_minutes", CODE_TTL_MINUTES)), 1),
            CODE_TTL_MAX_MINUTES,
        )
        expires_at = now + timedelta(minutes=ttl_minutes)
    max_uploads = min(
        max(int(data.get("max_uploads", MAX_UPLOADS_PER_SESSION)), 1),
        50,
    )

    # Génération côté interne — c'est le point clé
    simple_code = generate_simple_code()
    qr_token = generate_qr_token()

    db = SessionLocal()
    try:
        # Vérifier unicité (collision improbable mais on sécurise)
        for _ in range(5):
            existing = db.query(IssuedToken).filter(
                (IssuedToken.simple_code == simple_code) | (IssuedToken.qr_token == qr_token)
            ).first()
            if not existing:
                break
            simple_code = generate_simple_code()
            qr_token = generate_qr_token()

        token_record = IssuedToken(
            id=uuid4(),
            user_sub=user_sub,
            user_email=data.get("user_email"),
            user_display_name=data.get("user_display_name"),
            simple_code=simple_code,
            qr_token=qr_token,
            max_uploads=max_uploads,
            ttl_minutes=ttl_minutes,
            expires_at=expires_at,
            status_view_expires_at=expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES),
        )
        db.add(token_record)
        db.commit()

        logger.info(
            "Token issued: code=%s, user=%s, ttl=%dm, ttl_seconds=%s",
            simple_code, user_sub, ttl_minutes, ttl_seconds,
        )

        return jsonify({
            "token_id": str(token_record.id),
            "simple_code": simple_code,
            "qr_token": qr_token,
            "expires_at": token_record.expires_at.isoformat(),
            "ttl_minutes": ttl_minutes,
            "ttl_seconds": ttl_seconds,
            "max_uploads": max_uploads,
        })

    except Exception:
        db.rollback()
        logger.exception("Failed to issue token")
        return jsonify({"error": "Internal error"}), 500
    finally:
        db.close()


@app.route("/api/v1/validate-token/<simple_code>")
def validate_token(simple_code):
    """
    Vérifie qu'un token existe et est encore valide.
    Utilisable par le file-puller pour vérifier le matching.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401

    db = SessionLocal()
    try:
        token = db.query(IssuedToken).filter(
            IssuedToken.simple_code == simple_code.upper().strip()
        ).first()

        if not token:
            return jsonify({"valid": False, "reason": "not_found"}), 404

        now = datetime.now(timezone.utc)
        if token.expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"valid": False, "reason": "expired"}), 410

        return jsonify({
            "valid": True,
            "token_id": str(token.id),
            "user_sub": token.user_sub,
            "user_email": token.user_email,
            "max_uploads": token.max_uploads,
            "expires_at": token.expires_at.isoformat(),
        })

    finally:
        db.close()


@app.route("/api/v1/enroll-device", methods=["POST"])
def enroll_device():
    """Enroll a browser/device for a valid QR session."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    qr_token = (data.get("qr_token") or "").strip()
    device_key = (data.get("device_key") or "").strip()
    device_fingerprint = (data.get("device_fingerprint") or "").strip()
    device_name = (data.get("device_name") or "").strip() or None
    if not qr_token or not device_key:
        return jsonify({"error": "qr_token and device_key are required"}), 400

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        issued = db.query(IssuedToken).filter(IssuedToken.qr_token == qr_token).first()
        if not issued:
            return jsonify({"error": "invalid_qr_token"}), 404
        if issued.expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"error": "qr_token_expired"}), 410

        rec = (
            db.query(DeviceEnrollment)
            .filter(
                DeviceEnrollment.qr_token == qr_token,
                DeviceEnrollment.device_key == device_key,
            )
            .first()
        )
        retention_expires_at = now + timedelta(hours=DEVICE_TOKEN_RETENTION_HOURS)
        if rec:
            rec.status = "active"
            rec.revoked_at = None
            rec.revoked_reason = None
            rec.device_fingerprint = device_fingerprint or rec.device_fingerprint
            rec.device_name = device_name or rec.device_name
            rec.user_agent = request.headers.get("User-Agent", "")[:1024]
            rec.last_seen_at = now
            rec.retention_expires_at = retention_expires_at
            rec.updated_at = now
        else:
            rec = DeviceEnrollment(
                id=uuid4(),
                user_sub=issued.user_sub,
                qr_token=issued.qr_token,
                simple_code=issued.simple_code,
                device_key=device_key[:255],
                device_fingerprint=device_fingerprint[:1024],
                device_name=device_name[:255] if device_name else None,
                user_agent=(request.headers.get("User-Agent", "") or "")[:1024],
                status="active",
                retention_expires_at=retention_expires_at,
                last_seen_at=now,
            )
            db.add(rec)

        db.commit()
        payload = {
            "device_id": str(rec.id),
            "user_sub": rec.user_sub,
            "qr_token": rec.qr_token,
            "simple_code": rec.simple_code,
            "retention_until": int(rec.retention_expires_at.timestamp()),
            "iat": utc_now_ts(),
        }
        token = create_device_token(payload, INTERNAL_API_TOKEN)
        return jsonify(
            {
                "ok": True,
                "device_token": token,
                "device_id": str(rec.id),
                "retention_until": payload["retention_until"],
                "device_name": rec.device_name,
            }
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to enroll device")
        return jsonify({"error": "internal_error"}), 500
    finally:
        db.close()


@app.route("/api/v1/validate-device", methods=["POST"])
def validate_device():
    """Strong backend validation of a stateless device token."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    raw_token = (data.get("device_token") or "").strip()
    if not raw_token:
        return jsonify({"error": "device_token is required"}), 400

    try:
        payload = verify_device_token(raw_token, INTERNAL_API_TOKEN)
    except ValueError:
        return jsonify({"valid": False, "reason": "invalid_signature"}), 401

    device_id = (payload.get("device_id") or "").strip()
    qr_token = (payload.get("qr_token") or "").strip()
    retention_until = int(payload.get("retention_until") or 0)
    now_ts = utc_now_ts()
    if not device_id or not qr_token:
        return jsonify({"valid": False, "reason": "invalid_payload"}), 400
    if retention_until and now_ts > retention_until:
        return jsonify({"valid": False, "reason": "retention_expired"}), 410

    db = SessionLocal()
    try:
        rec = db.query(DeviceEnrollment).filter(DeviceEnrollment.id == device_id).first()
        if not rec:
            return jsonify({"valid": False, "reason": "not_found"}), 404
        if rec.status != "active":
            return jsonify({"valid": False, "reason": "revoked"}), 403
        if rec.qr_token != qr_token:
            return jsonify({"valid": False, "reason": "token_mismatch"}), 403

        now = datetime.now(timezone.utc)
        if rec.retention_expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"valid": False, "reason": "retention_expired"}), 410

        issued = db.query(IssuedToken).filter(IssuedToken.qr_token == qr_token).first()
        if not issued or issued.expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"valid": False, "reason": "qr_expired"}), 410

        rec.last_seen_at = now
        db.commit()
        return jsonify(
            {
                "valid": True,
                "device_id": str(rec.id),
                "user_sub": rec.user_sub,
                "retention_until": int(rec.retention_expires_at.timestamp()),
                "device_name": rec.device_name,
            }
        )
    finally:
        db.close()


@app.route("/api/v1/devices", methods=["GET"])
def list_devices():
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401

    user_sub = (request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400

    db = SessionLocal()
    try:
        devices = (
            db.query(DeviceEnrollment)
            .filter(DeviceEnrollment.user_sub == user_sub)
            .order_by(DeviceEnrollment.created_at.desc())
            .limit(200)
            .all()
        )
        return jsonify(
            [
                {
                    "device_id": str(d.id),
                    "user_sub": d.user_sub,
                    "simple_code": d.simple_code,
                    "qr_token": d.qr_token,
                    "device_key": d.device_key,
                    "device_name": d.device_name,
                    "device_fingerprint": d.device_fingerprint,
                    "status": d.status,
                    "retention_expires_at": d.retention_expires_at.isoformat() if d.retention_expires_at else None,
                    "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in devices
            ]
        )
    finally:
        db.close()


@app.route("/api/v1/devices/<device_id>/rename", methods=["POST"])
def rename_device(device_id: str):
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    new_name = (data.get("device_name") or "").strip()
    if not user_sub or not new_name:
        return jsonify({"error": "user_sub and device_name are required"}), 400

    db = SessionLocal()
    try:
        rec = db.query(DeviceEnrollment).filter(DeviceEnrollment.id == device_id).first()
        if not rec or rec.user_sub != user_sub:
            return jsonify({"error": "not_found"}), 404
        rec.device_name = new_name[:255]
        rec.updated_at = datetime.now(timezone.utc)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/v1/devices/<device_id>/revoke", methods=["POST"])
def revoke_device(device_id: str):
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    reason = (data.get("reason") or "revoked_by_user").strip()[:255]
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400

    db = SessionLocal()
    try:
        rec = db.query(DeviceEnrollment).filter(DeviceEnrollment.id == device_id).first()
        if not rec or rec.user_sub != user_sub:
            return jsonify({"error": "not_found"}), 404
        rec.status = "revoked"
        rec.revoked_reason = reason
        rec.revoked_at = datetime.now(timezone.utc)
        rec.updated_at = rec.revoked_at
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/v1/devices/revoke-all", methods=["POST"])
def revoke_all_devices():
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    reason = (data.get("reason") or "revoked_all_by_user").strip()[:255]
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400
    now = datetime.now(timezone.utc)

    db = SessionLocal()
    try:
        updated = (
            db.query(DeviceEnrollment)
            .filter(DeviceEnrollment.user_sub == user_sub, DeviceEnrollment.status == "active")
            .update(
                {
                    DeviceEnrollment.status: "revoked",
                    DeviceEnrollment.revoked_reason: reason,
                    DeviceEnrollment.revoked_at: now,
                    DeviceEnrollment.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return jsonify({"ok": True, "revoked": int(updated)})
    finally:
        db.close()


@app.route("/api/v1/devices/admin/revoke-all", methods=["POST"])
def admin_revoke_all_devices():
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "revoked_by_admin").strip()[:255]
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        updated = (
            db.query(DeviceEnrollment)
            .filter(DeviceEnrollment.status == "active")
            .update(
                {
                    DeviceEnrollment.status: "revoked",
                    DeviceEnrollment.revoked_reason: reason,
                    DeviceEnrollment.revoked_at: now,
                    DeviceEnrollment.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return jsonify({"ok": True, "revoked": int(updated)})
    finally:
        db.close()


# ─── Init ───────────────────────────────────────────────────

def create_app():
    global SessionLocal
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    try:
        init_tables(db_cfg, InternalBase)
    except Exception as exc:
        # Gunicorn workers may race on create_all at boot. If table creation
        # already succeeded in another worker, keep booting.
        if "pg_type_typname_nsp_index" in str(exc) or "already exists" in str(exc):
            logger.warning("Schema init race detected, continuing startup: %s", exc)
        else:
            raise
    SessionLocal = create_session_factory(db_cfg)
    return app


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port = int(os.getenv("TOKEN_ISSUER_PORT", 8091))
    application.run(host="0.0.0.0", port=port)
