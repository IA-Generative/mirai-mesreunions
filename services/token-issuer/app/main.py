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
from libs.shared.app.models import InternalBase, IssuedToken, DeviceEnrollment, IssuedTokenOption, OidcRefreshToken
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token
from libs.shared.app.device_token import create_device_token, verify_device_token, utc_now_ts
from libs.shared.app.device_fingerprint import compute_fp_hash

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

db_cfg = load_int_db()
SessionLocal = None
ALLOW_SHORT_QR_TTL_SECONDS_TEST = os.getenv("ALLOW_SHORT_QR_TTL_SECONDS_TEST", "").lower() in {"1", "true", "yes"}
DEVICE_TOKEN_RETENTION_HOURS = max(1, int(os.getenv("DEVICE_TOKEN_RETENTION_HOURS", "168")))
TOKEN_RENEW_DAYS = max(1, int(os.getenv("TOKEN_RENEW_DAYS", "7")))
DEVICE_FUSION_WINDOW_MINUTES = max(1, int(os.getenv("DEVICE_FUSION_WINDOW_MINUTES", "15")))
DEVICE_PENDING_PURGE_SECONDS = max(60, int(os.getenv("DEVICE_PENDING_PURGE_SECONDS", "120")))


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


def purge_expired_pending(db) -> int:
    """Delete `pending` device rows whose purge_at has elapsed. Returns row count."""
    now = datetime.now(timezone.utc)
    deleted = (
        db.query(DeviceEnrollment)
        .filter(
            DeviceEnrollment.status == "pending",
            DeviceEnrollment.purge_at.isnot(None),
            DeviceEnrollment.purge_at < now,
        )
        .delete(synchronize_session=False)
    )
    if deleted:
        db.commit()
    return int(deleted or 0)


# ─── Routes ─────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "token-issuer", "zone": "internal"})


@app.route("/healthz")
def healthz():
    return health()


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
    auto_transcribe = bool(data.get("auto_transcribe", True))

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
        db.add(
            IssuedTokenOption(
                id=uuid4(),
                qr_token=qr_token,
                simple_code=simple_code,
                user_sub=user_sub,
                auto_transcribe=auto_transcribe,
            )
        )
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
            "auto_transcribe": auto_transcribe,
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


@app.route("/api/v1/tokens/extend-7d", methods=["POST"])
def extend_token_7d():
    """Extend a QR token validity and optionally increase upload quota."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    qr_token = (data.get("qr_token") or "").strip()
    if not user_sub or not qr_token:
        return jsonify({"error": "user_sub and qr_token are required"}), 400
    ttl_minutes_raw = data.get("ttl_minutes")
    ttl_seconds_raw = data.get("ttl_seconds")
    add_uploads_raw = data.get("add_uploads", 0)

    ttl_seconds = None
    ttl_minutes = TOKEN_RENEW_DAYS * 24 * 60
    if ttl_seconds_raw is not None:
        try:
            ttl_seconds = int(ttl_seconds_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "ttl_seconds must be an integer"}), 400
        if not ALLOW_SHORT_QR_TTL_SECONDS_TEST:
            return jsonify({"error": "Short TTL test mode is disabled"}), 400
        if ttl_seconds not in {15, 30}:
            return jsonify({"error": "Allowed ttl_seconds values are 15 or 30"}), 400
        ttl_minutes = max(1, math.ceil(ttl_seconds / 60))
    elif ttl_minutes_raw is not None:
        try:
            ttl_minutes = int(ttl_minutes_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "ttl_minutes must be an integer"}), 400
        ttl_minutes = min(max(ttl_minutes, 1), CODE_TTL_MAX_MINUTES)

    try:
        add_uploads = int(add_uploads_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "add_uploads must be an integer"}), 400
    add_uploads = max(add_uploads, 0)

    db = SessionLocal()
    try:
        token = db.query(IssuedToken).filter(IssuedToken.qr_token == qr_token).first()
        if not token:
            return jsonify({"error": "not_found"}), 404
        if token.user_sub != user_sub:
            return jsonify({"error": "forbidden"}), 403

        now = datetime.now(timezone.utc)
        base = token.expires_at.replace(tzinfo=timezone.utc)
        if base < now:
            base = now
        if ttl_seconds is not None:
            token.expires_at = base + timedelta(seconds=ttl_seconds)
        else:
            token.expires_at = base + timedelta(minutes=ttl_minutes)
        token.status_view_expires_at = token.expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES)
        if add_uploads > 0:
            token.max_uploads = min(1000, int(token.max_uploads or 0) + add_uploads)
        db.commit()

        return jsonify({
            "ok": True,
            "qr_token": token.qr_token,
            "expires_at": token.expires_at.isoformat(),
            "status_view_expires_at": token.status_view_expires_at.isoformat() if token.status_view_expires_at else None,
            "renew_days": TOKEN_RENEW_DAYS,
            "ttl_minutes": ttl_minutes,
            "ttl_seconds": ttl_seconds,
            "max_uploads": token.max_uploads,
            "add_uploads": add_uploads,
        })
    except Exception:
        db.rollback()
        logger.exception("Failed to extend token validity")
        return jsonify({"error": "internal_error"}), 500
    finally:
        db.close()


@app.route("/api/v1/enroll-device", methods=["POST"])
def enroll_device():
    """
    Enroll a browser/device for a valid QR session.

    Lookup order (1 QR = 1 device, with browser↔PWA fusion):
      1. Same (qr_token, device_key)         → idempotent re-enroll, reuse row.
      2. Same qr_token + matching fp_hash    → fusion (browser → PWA install).
         Only within DEVICE_FUSION_WINDOW_MINUTES from the existing row's
         created_at, regardless of pending/active. Returns the existing row's
         token; the caller's device_key is ignored server-side.
      3. Confirmed device exists on qr_token → reject (409 already_bound).
      4. Otherwise                           → create new pending row, with a
         short purge_at so dead enrolments disappear quickly.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    qr_token = (data.get("qr_token") or "").strip()
    device_key = (data.get("device_key") or "").strip()
    device_fingerprint = (data.get("device_fingerprint") or "").strip()
    device_name = (data.get("device_name") or "").strip() or None
    if not qr_token or not device_key:
        return jsonify({"error": "qr_token and device_key are required"}), 400

    fp_hash = compute_fp_hash(device_fingerprint)
    user_agent = (request.headers.get("User-Agent", "") or "")[:1024]

    db = SessionLocal()
    try:
        # Opportunistic cleanup of any expired pending rows on every enrol.
        try:
            purge_expired_pending(db)
        except Exception:
            db.rollback()

        now = datetime.now(timezone.utc)
        issued = db.query(IssuedToken).filter(IssuedToken.qr_token == qr_token).first()
        if not issued:
            return jsonify({"error": "invalid_qr_token"}), 404
        if issued.expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"error": "qr_token_expired"}), 410

        retention_expires_at = now + timedelta(hours=DEVICE_TOKEN_RETENTION_HOURS)
        purge_at = now + timedelta(seconds=DEVICE_PENDING_PURGE_SECONDS)
        fusion_cutoff = now - timedelta(minutes=DEVICE_FUSION_WINDOW_MINUTES)
        enroll_reason = "fresh"

        # 1. Idempotent re-enroll: same (qr_token, device_key)
        rec = (
            db.query(DeviceEnrollment)
            .filter(
                DeviceEnrollment.qr_token == qr_token,
                DeviceEnrollment.device_key == device_key,
            )
            .first()
        )
        if rec and rec.status == "revoked":
            return jsonify({"error": "device_revoked"}), 403
        if rec:
            enroll_reason = "idempotent"

        # 2. Fusion (browser → PWA install): same qr_token, matching fp_hash,
        #    within fusion window. Skip revoked rows.
        if not rec and fp_hash:
            rec = (
                db.query(DeviceEnrollment)
                .filter(
                    DeviceEnrollment.qr_token == qr_token,
                    DeviceEnrollment.fp_hash == fp_hash,
                    DeviceEnrollment.status != "revoked",
                    DeviceEnrollment.created_at > fusion_cutoff,
                )
                .order_by(DeviceEnrollment.created_at.desc())
                .first()
            )
            if rec:
                enroll_reason = "fused_fp_window"

        if rec:
            # Refresh metadata; do NOT change confirmed_at here (only heartbeat
            # or upload confirms). Extend retention/purge windows.
            rec.device_fingerprint = device_fingerprint[:1024] or rec.device_fingerprint
            if not rec.fp_hash and fp_hash:
                rec.fp_hash = fp_hash
            rec.device_name = (device_name[:255] if device_name else rec.device_name)
            rec.user_agent = user_agent or rec.user_agent
            rec.last_seen_at = now
            rec.retention_expires_at = retention_expires_at
            rec.updated_at = now
            if rec.status == "pending":
                rec.purge_at = purge_at
        else:
            # 3. Reject if a confirmed device already owns this qr_token.
            confirmed = (
                db.query(DeviceEnrollment)
                .filter(
                    DeviceEnrollment.qr_token == qr_token,
                    DeviceEnrollment.status == "active",
                    DeviceEnrollment.confirmed_at.isnot(None),
                )
                .first()
            )
            if confirmed:
                return jsonify({
                    "error": "session_already_bound",
                    "message": (
                        "Cette session est deja liee a un autre appareil. "
                        "Pour changer d'appareil, revoquez l'enrolement depuis le portail mydevices."
                    ),
                }), 409

            # 4. New pending enrolment.
            rec = DeviceEnrollment(
                id=uuid4(),
                user_sub=issued.user_sub,
                qr_token=issued.qr_token,
                simple_code=issued.simple_code,
                device_key=device_key[:255],
                device_fingerprint=device_fingerprint[:1024],
                fp_hash=fp_hash or None,
                device_name=device_name[:255] if device_name else None,
                user_agent=user_agent,
                status="pending",
                confirmed_at=None,
                purge_at=purge_at,
                retention_expires_at=retention_expires_at,
                last_seen_at=now,
            )
            db.add(rec)

        db.commit()
        logger.info(
            "Device enrol: qr=%s reason=%s status=%s device_id=%s fp_hash=%s",
            qr_token, enroll_reason, rec.status, rec.id, (fp_hash or "-")[:8],
        )

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
                "status": rec.status,
                "enroll_reason": enroll_reason,
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
        if rec.status == "revoked":
            return jsonify({"valid": False, "reason": "revoked"}), 403
        if rec.qr_token != qr_token:
            return jsonify({"valid": False, "reason": "token_mismatch"}), 403

        now = datetime.now(timezone.utc)
        if rec.retention_expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"valid": False, "reason": "retention_expired"}), 410

        issued = db.query(IssuedToken).filter(IssuedToken.qr_token == qr_token).first()
        if not issued or issued.expires_at.replace(tzinfo=timezone.utc) < now:
            return jsonify({"valid": False, "reason": "qr_expired"}), 410

        # Pending → active transition on first heartbeat. The first device to
        # reach this point owns the qr_token; concurrent pending peers are
        # rejected here and will be purged at their purge_at deadline.
        if rec.status == "pending":
            other_active = (
                db.query(DeviceEnrollment)
                .filter(
                    DeviceEnrollment.qr_token == qr_token,
                    DeviceEnrollment.status == "active",
                    DeviceEnrollment.confirmed_at.isnot(None),
                    DeviceEnrollment.id != rec.id,
                )
                .first()
            )
            if other_active:
                return jsonify({"valid": False, "reason": "session_already_bound"}), 409
            rec.status = "active"
            rec.confirmed_at = now
            rec.purge_at = None

        rec.last_seen_at = now
        db.commit()
        return jsonify(
            {
                "valid": True,
                "device_id": str(rec.id),
                "user_sub": rec.user_sub,
                "retention_until": int(rec.retention_expires_at.timestamp()),
                "device_name": rec.device_name,
                "status": rec.status,
                "confirmed_at": rec.confirmed_at.isoformat() if rec.confirmed_at else None,
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
        # Lazy purge of expired pending rows so callers never see them.
        try:
            purge_expired_pending(db)
        except Exception:
            db.rollback()

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
                    "fp_hash": d.fp_hash,
                    "status": d.status,
                    "confirmed_at": d.confirmed_at.isoformat() if d.confirmed_at else None,
                    "purge_at": d.purge_at.isoformat() if d.purge_at else None,
                    "revoked_at": d.revoked_at.isoformat() if d.revoked_at else None,
                    "revoked_reason": d.revoked_reason,
                    "retention_expires_at": d.retention_expires_at.isoformat() if d.retention_expires_at else None,
                    "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "updated_at": d.updated_at.isoformat() if d.updated_at else None,
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


@app.route("/api/v1/sessions/<simple_code>", methods=["DELETE"])
def delete_session(simple_code: str):
    """Permanently remove an issued token + its options + any linked devices.

    Used by code-generator when the user clicks "Supprimer cette session" on
    a session card. Cascades:
      - issued_token_options (FK on simple_code)
      - device_enrollments  (FK on simple_code, may be 0 rows for never-enrolled codes)
      - issued_tokens (the source row)

    Auth: INTERNAL_API_TOKEN bearer. Body: ``{"user_sub": "..."}`` for the
    ownership check; mismatch → 404 (no info leak).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400

    db = SessionLocal()
    try:
        token = (
            db.query(IssuedToken)
            .filter(IssuedToken.simple_code == simple_code)
            .first()
        )
        if not token or token.user_sub != user_sub:
            return jsonify({"error": "not_found"}), 404
        # Cascade in this order so we never leave dangling FKs.
        n_devices = (
            db.query(DeviceEnrollment)
            .filter(DeviceEnrollment.simple_code == simple_code)
            .delete(synchronize_session=False)
        )
        n_options = (
            db.query(IssuedTokenOption)
            .filter(IssuedTokenOption.simple_code == simple_code)
            .delete(synchronize_session=False)
        )
        db.delete(token)
        db.commit()
        logger.info(
            "Session %s permanently deleted (user_sub=%s, devices=%d, options=%d)",
            simple_code, user_sub, n_devices, n_options,
        )
        return jsonify({"deleted": True, "devices_removed": n_devices})
    finally:
        db.close()


@app.route("/api/v1/devices/<device_id>", methods=["DELETE"])
def delete_device(device_id: str):
    """Permanently remove a device enrollment row.

    Stronger than revoke (which keeps the row in DB for audit + hides it
    after 24h). Use sparingly — once deleted, any historical reference
    by device_id will return 404.

    Auth: INTERNAL_API_TOKEN bearer (same as the other internal endpoints).
    Body: ``{"user_sub": "..."}`` — ownership check; mismatched user_sub
    returns 404 (no leak about whether the device exists for someone else).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400

    db = SessionLocal()
    try:
        rec = db.query(DeviceEnrollment).filter(DeviceEnrollment.id == device_id).first()
        if not rec or rec.user_sub != user_sub:
            return jsonify({"error": "not_found"}), 404
        prior_status = rec.status
        prior_qr = rec.qr_token
        db.delete(rec)
        db.commit()
        logger.info("Device %s permanently deleted (user_sub=%s prior_status=%s qr_token=%s…)",
                    device_id, user_sub, prior_status, (prior_qr or "")[:8])
        return jsonify({"deleted": True})
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


@app.route("/api/v1/oidc-refresh-store", methods=["POST"])
def oidc_refresh_store():
    """
    UPSERT a (Fernet-encrypted) OIDC refresh token, keyed by user_sub.

    Called by code-generator and admin-portal after a successful OIDC login
    when offline_access was requested. The plaintext token is never sent in
    the body — the caller has already encrypted it with the shared Fernet
    key via libs.shared.app.secrets_crypto.

    Body: { user_sub, ciphertext, keycloak_iss?, user_email? }
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    ciphertext = (data.get("ciphertext") or "").strip()
    keycloak_iss = (data.get("keycloak_iss") or "").strip()[:512] or None
    user_email = (data.get("user_email") or "").strip()[:255] or None
    if not user_sub or not ciphertext:
        return jsonify({"error": "user_sub and ciphertext are required"}), 400

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        rec = db.query(OidcRefreshToken).filter(OidcRefreshToken.user_sub == user_sub).first()
        if rec:
            rec.ciphertext = ciphertext
            rec.keycloak_iss = keycloak_iss
            rec.user_email = user_email
            rec.last_login_at = now
            rec.updated_at = now
            action = "update"
        else:
            db.add(OidcRefreshToken(
                user_sub=user_sub,
                ciphertext=ciphertext,
                keycloak_iss=keycloak_iss,
                user_email=user_email,
                last_login_at=now,
            ))
            action = "insert"
        db.commit()
        logger.info("oidc_refresh_store: %s for user_sub=%s (iss=%s)", action, user_sub, keycloak_iss or "-")
        return jsonify({"ok": True, "action": action})
    except Exception:
        db.rollback()
        logger.exception("oidc_refresh_store: UPSERT failed for user_sub=%s", user_sub)
        return jsonify({"error": "internal_error"}), 500
    finally:
        db.close()


@app.route("/api/v1/oidc-refresh-fetch/<user_sub>", methods=["GET"])
def oidc_refresh_fetch(user_sub: str):
    """
    Return the stored ciphertext for a given user_sub (or 404).

    Used by file-puller at MCR push time. The decryption happens
    file-puller-side, so the Fernet key only needs to be present there
    (and on CG/admin which encrypt). token-issuer is key-blind.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    user_sub = (user_sub or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400
    db = SessionLocal()
    try:
        rec = db.query(OidcRefreshToken).filter(OidcRefreshToken.user_sub == user_sub).first()
        if not rec:
            return jsonify({"error": "not_found"}), 404
        return jsonify({
            "user_sub": rec.user_sub,
            "ciphertext": rec.ciphertext,
            "keycloak_iss": rec.keycloak_iss,
            "user_email": rec.user_email,
            "last_login_at": rec.last_login_at.isoformat() if rec.last_login_at else None,
        })
    finally:
        db.close()


@app.route("/api/v1/oidc-refresh-delete/<user_sub>", methods=["DELETE"])
def oidc_refresh_delete(user_sub: str):
    """
    Delete the stored refresh token for a user_sub. Called by file-puller
    when Keycloak responds invalid_grant (refresh expired/revoked) so the
    next MCR push attempt fails fast in mcr_auth_failed without trying to
    use a known-bad token.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    user_sub = (user_sub or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub is required"}), 400
    db = SessionLocal()
    try:
        deleted = (
            db.query(OidcRefreshToken)
            .filter(OidcRefreshToken.user_sub == user_sub)
            .delete(synchronize_session=False)
        )
        db.commit()
        return jsonify({"ok": True, "deleted": int(deleted)})
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
