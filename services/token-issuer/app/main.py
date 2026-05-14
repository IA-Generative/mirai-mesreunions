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
from libs.shared.app.models import InternalBase, IssuedToken, DeviceEnrollment, IssuedTokenOption, OidcRefreshToken, UserAudioFile, TranscriptionEvent, MeetingBrief
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
    # Défaut : aligner sur DEVICE_TOKEN_RETENTION_HOURS (15j en prod-bêta).
    # Précédemment on étendait de TOKEN_RENEW_DAYS (7j) ce qui désynchro­
    # nisait QR (7j) vs device retention (15j) → validate_device finissait
    # par retourner "qr_expired" alors que le device était encore valide.
    ttl_minutes = DEVICE_TOKEN_RETENTION_HOURS * 60
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

        # Étend aussi la rétention des devices enrôlés sur ce QR. Sans
        # ça, "Renouveler" ne déplaçait que la fenêtre d'enrôlement (QR)
        # qui n'a plus d'importance une fois enrôlé — le device perdait
        # son accès au bout de 15j même si l'utilisateur cliquait
        # Renouveler 20× entre temps. On bump retention_expires_at à
        # now + DEVICE_TOKEN_RETENTION_HOURS pour tous les devices
        # actifs liés à ce token.
        new_retention = now + timedelta(hours=DEVICE_TOKEN_RETENTION_HOURS)
        devices_bumped = (
            db.query(DeviceEnrollment)
            .filter(
                DeviceEnrollment.qr_token == qr_token,
                DeviceEnrollment.status == "active",
            )
            .update(
                {"retention_expires_at": new_retention},
                synchronize_session=False,
            )
        )
        db.commit()
        if devices_bumped:
            logger.info(
                "Token %s renewed: %d device(s) retention extended to %s",
                qr_token, devices_bumped, new_retention.isoformat(),
            )

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

        # 2b. Rebind par fingerprint (sans fenêtre temporelle) : un device
        #     active+confirmed existe sur ce qr_token avec le MÊME fp_hash
        #     que la requête entrante, mais le device_key ne matche pas.
        #     C'est typiquement le cas "PWA a perdu son localStorage" :
        #     - l'utilisateur a fait un Reset / clear data / réinstall PWA,
        #     - ou son SW cache a été purgé,
        #     - ou iOS a évincé son localStorage.
        #     Dans tous ces cas l'utilisateur est légitime (il a accès au
        #     qr_token URL, qui est le secret d'accès), il a juste perdu
        #     son token côté client. On rebind donc à la row existante en
        #     rotant le device_key. Audit log warning pour détecter les
        #     abus si jamais le modèle de menace évolue.
        if not rec and fp_hash:
            rebind_candidate = (
                db.query(DeviceEnrollment)
                .filter(
                    DeviceEnrollment.qr_token == qr_token,
                    DeviceEnrollment.fp_hash == fp_hash,
                    DeviceEnrollment.status == "active",
                    DeviceEnrollment.confirmed_at.isnot(None),
                )
                .first()
            )
            if rebind_candidate:
                old_device_key = (rebind_candidate.device_key or "")[:12]
                old_device_id = str(rebind_candidate.id)
                rec = rebind_candidate
                # Rotate device_key : on accepte la nouvelle valeur générée
                # côté client. Le HMAC du device_token sera invalidé pour
                # quiconque détient l'ancien device_key (sécurité par
                # rotation, pas par révocation).
                rec.device_key = device_key[:255]
                enroll_reason = "rebind_fp"
                logger.warning(
                    "Device rebind by fingerprint match: device_id=%s qr_token=%s "
                    "old_device_key=%s… new_device_key=%s… fp_hash=%s…",
                    old_device_id, qr_token, old_device_key,
                    device_key[:12], fp_hash[:12],
                )

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

        # Re-issue le device_token quand le retention_until figé dans le
        # token diverge de la valeur DB (cas typique : l'utilisateur a
        # cliqué « Renouveler » côté mydevices, on a bumpé
        # retention_expires_at à now+360h, mais le token JWT que le
        # téléphone conserve dans localStorage porte encore l'ancienne
        # valeur → la PWA affiche l'ancienne expiration). On renvoie un
        # `refreshed_device_token` que la PWA pourra setter en
        # localStorage à la prochaine heartbeat.
        refreshed_device_token = None
        db_retention_ts = int(rec.retention_expires_at.timestamp())
        if retention_until != db_retention_ts:
            try:
                fresh_payload = dict(payload)
                fresh_payload["retention_until"] = db_retention_ts
                refreshed_device_token = create_device_token(fresh_payload, INTERNAL_API_TOKEN)
            except Exception:
                logger.exception("Failed to refresh device_token for %s", rec.id)
                refreshed_device_token = None

        return jsonify(
            {
                "valid": True,
                "device_id": str(rec.id),
                "user_sub": rec.user_sub,
                "retention_until": db_retention_ts,
                "device_name": rec.device_name,
                "status": rec.status,
                "confirmed_at": rec.confirmed_at.isoformat() if rec.confirmed_at else None,
                "refreshed_device_token": refreshed_device_token,
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


@app.route("/api/v1/files/by-session", methods=["DELETE"])
def delete_file_by_session():
    """Permanently remove a user_audio_files row + its transcription_events.

    Called by code-generator quand l'utilisateur clique "Supprimer ce
    fichier" depuis mydevices. La suppression côté externe (uploaded_files
    + S3) est faite par code-generator avant cet appel ; cette route ne
    s'occupe que de la zone interne.

    Le lookup se fait sur ``(user_sub, simple_code, original_filename)`` :
    user_audio_files.id n'est PAS lié à uploaded_files.id (ce sont deux
    UUID indépendants). Le tuple ci-dessus est porté par
    ``user_audio_files.original_session_code`` + ``original_filename``.

    Auth: INTERNAL_API_TOKEN bearer.
    Body: ``{"user_sub": "...", "simple_code": "...", "original_filename": "..."}``
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    simple_code = (data.get("simple_code") or "").strip()
    original_filename = (data.get("original_filename") or "").strip()
    if not user_sub or not simple_code or not original_filename:
        return jsonify({
            "error": "user_sub, simple_code and original_filename are required"
        }), 400

    db = SessionLocal()
    try:
        audio_files = (
            db.query(UserAudioFile)
            .filter(
                UserAudioFile.user_sub == user_sub,
                UserAudioFile.original_session_code == simple_code,
                UserAudioFile.original_filename == original_filename,
            )
            .all()
        )
        if not audio_files:
            return jsonify({"error": "not_found"}), 404
        n_events_total = 0
        for af in audio_files:
            n_events_total += (
                db.query(TranscriptionEvent)
                .filter(TranscriptionEvent.audio_file_id == af.id)
                .delete(synchronize_session=False)
            )
            db.delete(af)
        db.commit()
        logger.info(
            "Internal audio file row(s) deleted (user_sub=%s, simple_code=%s, "
            "filename=%s, rows=%d, events=%d)",
            user_sub, simple_code, original_filename, len(audio_files), n_events_total,
        )
        return jsonify({
            "deleted": True,
            "rows_removed": len(audio_files),
            "events_removed": n_events_total,
        })
    finally:
        db.close()


@app.route("/api/v1/files/by-session/rename", methods=["POST"])
def rename_file_by_session():
    """Renomme le titre suggéré (suggested_filename) d'un user_audio_files.

    Auth: INTERNAL_API_TOKEN bearer.
    Body: ``{"user_sub","simple_code","original_filename","new_title"}``
    Matching identique à delete_file_by_session.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    simple_code = (data.get("simple_code") or "").strip()
    original_filename = (data.get("original_filename") or "").strip()
    new_title = (data.get("new_title") or "").strip()
    if not user_sub or not simple_code or not original_filename or not new_title:
        return jsonify({
            "error": "user_sub, simple_code, original_filename and new_title are required"
        }), 400
    new_title = new_title[:500]  # safety cap

    db = SessionLocal()
    try:
        rows = (
            db.query(UserAudioFile)
            .filter(
                UserAudioFile.user_sub == user_sub,
                UserAudioFile.original_session_code == simple_code,
                UserAudioFile.original_filename == original_filename,
            )
            .all()
        )
        if not rows:
            return jsonify({"error": "not_found"}), 404
        for af in rows:
            af.suggested_filename = new_title
        db.commit()
        logger.info(
            "User audio file renamed: user_sub=%s simple_code=%s file=%s → '%s' (%d rows)",
            user_sub, simple_code, original_filename, new_title[:80], len(rows),
        )
        return jsonify({"ok": True, "rows_updated": len(rows), "new_title": new_title})
    finally:
        db.close()


@app.route("/api/v1/files/by-session/meeting-datetime", methods=["POST"])
def set_meeting_datetime_by_session():
    """Surcharge la date/heure de réunion (UserAudioFile.meeting_datetime).

    Auth: INTERNAL_API_TOKEN bearer.
    Body: ``{"user_sub","simple_code","original_filename","meeting_datetime": "ISO 8601" | null}``
    Matching identique à rename_file_by_session. ``meeting_datetime = null``
    efface l'override (retour à la date d'upload côté UI).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    simple_code = (data.get("simple_code") or "").strip()
    original_filename = (data.get("original_filename") or "").strip()
    raw_dt = data.get("meeting_datetime")
    if not user_sub or not simple_code or not original_filename:
        return jsonify({
            "error": "user_sub, simple_code and original_filename are required"
        }), 400

    new_dt = None
    if raw_dt is not None:
        if not isinstance(raw_dt, str) or not raw_dt.strip():
            return jsonify({"error": "meeting_datetime must be an ISO 8601 string or null"}), 400
        try:
            parsed = datetime.fromisoformat(raw_dt.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            new_dt = parsed
        except Exception:
            return jsonify({"error": "meeting_datetime not parseable as ISO 8601"}), 400

    db = SessionLocal()
    try:
        rows = (
            db.query(UserAudioFile)
            .filter(
                UserAudioFile.user_sub == user_sub,
                UserAudioFile.original_session_code == simple_code,
                UserAudioFile.original_filename == original_filename,
            )
            .all()
        )
        if not rows:
            return jsonify({"error": "not_found"}), 404
        for af in rows:
            af.meeting_datetime = new_dt
        db.commit()
        logger.info(
            "Meeting datetime updated: user_sub=%s simple_code=%s file=%s → %s (%d rows)",
            user_sub, simple_code, original_filename, new_dt.isoformat() if new_dt else None, len(rows),
        )
        return jsonify({
            "ok": True,
            "rows_updated": len(rows),
            "meeting_datetime": new_dt.isoformat() if new_dt else None,
        })
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


# ─── MeetingBrief CRUD (zone interne) ───────────────────────
#
# La table meeting_briefs vit en zone INTERNE (postgres-internal) — comme
# user_audio_files. Code-generator (zone externe) ne peut pas l'attaquer
# directement ; il relaie via les endpoints ci-dessous, exactement comme
# il le fait pour rename/delete des fichiers (cf. rename_file_by_session,
# delete_file_by_session).
#
# Toutes les routes sont authentifiées par INTERNAL_API_TOKEN bearer.
# L'isolation utilisateur reste à la charge du serveur : chaque appel
# transporte explicitement ``user_sub`` et tout lookup le filtre.

def _brief_to_dict(b: MeetingBrief, *, with_full: bool = False) -> dict:
    """Sérialise un MeetingBrief pour la réponse JSON.

    ``with_full=False`` produit la vue listing (pas de brief_json/documents
    pour ne pas alourdir le payload). ``with_full=True`` ajoute les champs
    lourds pour la vue détail.
    """
    out = {
        "id": str(b.id),
        "user_sub": b.user_sub,
        "subject": b.subject,
        "title": b.title or b.subject,
        "role": b.role,
        "expectation": b.expectation,
        "duration_minutes": b.duration_minutes,
        "drive_folder_id": b.drive_folder_id,
        "focus": b.focus,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
        "trashed_at": b.trashed_at.isoformat() if b.trashed_at else None,
    }
    if with_full:
        out["brief_json"] = b.brief_json
        out["documents"] = b.documents
    return out


@app.route("/api/v1/briefs", methods=["POST"])
def create_brief():
    """Persiste un nouveau brief de réunion.

    Auth: INTERNAL_API_TOKEN. Body: ``user_sub`` + champs du brief
    (subject, role, expectation, focus, duration_minutes, drive_folder_id,
    brief_json, documents, title).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400

    db = SessionLocal()
    try:
        b = MeetingBrief(
            user_sub=user_sub,
            subject=(data.get("subject") or None),
            drive_folder_id=(data.get("drive_folder_id") or None),
            role=(data.get("role") or None),
            expectation=(data.get("expectation") or None),
            focus=data.get("focus"),
            duration_minutes=data.get("duration_minutes"),
            brief_json=data.get("brief_json"),
            documents=data.get("documents"),
            title=(data.get("title") or data.get("subject") or None),
        )
        db.add(b)
        db.commit()
        db.refresh(b)
        logger.info("MeetingBrief created: id=%s user_sub=%s", b.id, user_sub)
        return jsonify({"ok": True, "brief": _brief_to_dict(b, with_full=True)})
    finally:
        db.close()


@app.route("/api/v1/briefs", methods=["GET"])
def list_briefs():
    """Liste les briefs actifs ou en corbeille pour ``user_sub``.

    Query: ``user_sub`` (obligatoire), ``trashed`` (``true``|``false``,
    défaut ``false``), ``limit`` (défaut 50).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    user_sub = (request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    trashed_flag = (request.args.get("trashed") or "false").lower() in {"1", "true", "yes"}
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 50)))
    except ValueError:
        limit = 50

    db = SessionLocal()
    try:
        q = db.query(MeetingBrief).filter(MeetingBrief.user_sub == user_sub)
        if trashed_flag:
            q = q.filter(MeetingBrief.trashed_at.isnot(None)).order_by(MeetingBrief.trashed_at.desc())
        else:
            q = q.filter(MeetingBrief.trashed_at.is_(None)).order_by(MeetingBrief.created_at.desc())
        rows = q.limit(limit).all()
        return jsonify({"briefs": [_brief_to_dict(b) for b in rows]})
    finally:
        db.close()


@app.route("/api/v1/briefs/<brief_id>", methods=["GET"])
def get_brief(brief_id: str):
    """Lecture détaillée d'un brief (404 si trashed ou autre user_sub)."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    user_sub = (request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400

    db = SessionLocal()
    try:
        b = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.id == brief_id,
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.is_(None),
            )
            .first()
        )
        if not b:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"brief": _brief_to_dict(b, with_full=True)})
    finally:
        db.close()


@app.route("/api/v1/briefs/<brief_id>/rename", methods=["POST"])
def rename_brief(brief_id: str):
    """Renomme le titre d'un brief (≤120 car., contrat identique aux fichiers)."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    new_title = (data.get("title") or "").strip()
    if not user_sub or not new_title:
        return jsonify({"error": "user_sub and title required"}), 400
    new_title = new_title[:120]

    db = SessionLocal()
    try:
        b = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.id == brief_id,
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.is_(None),
            )
            .first()
        )
        if not b:
            return jsonify({"error": "not_found"}), 404
        b.title = new_title
        db.commit()
        return jsonify({"ok": True, "title": new_title})
    finally:
        db.close()


@app.route("/api/v1/briefs/<brief_id>/amend", methods=["POST"])
def amend_brief(brief_id: str):
    """Édition manuelle des champs ``brief_json`` (option a — pas de ré-appel LLM).

    Body: ``user_sub`` + ``brief_json`` (dict complet à substituer).
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    if "brief_json" not in data:
        return jsonify({"error": "brief_json required"}), 400
    new_brief_json = data.get("brief_json")
    if not isinstance(new_brief_json, dict):
        return jsonify({"error": "brief_json must be an object"}), 400

    db = SessionLocal()
    try:
        b = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.id == brief_id,
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.is_(None),
            )
            .first()
        )
        if not b:
            return jsonify({"error": "not_found"}), 404
        b.brief_json = new_brief_json
        db.commit()
        db.refresh(b)
        return jsonify({"ok": True, "brief": _brief_to_dict(b, with_full=True)})
    finally:
        db.close()


@app.route("/api/v1/briefs/<brief_id>", methods=["DELETE"])
def trash_brief(brief_id: str):
    """Soft-delete : positionne ``trashed_at = now()``."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400

    db = SessionLocal()
    try:
        b = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.id == brief_id,
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.is_(None),
            )
            .first()
        )
        if not b:
            return jsonify({"error": "not_found"}), 404
        b.trashed_at = datetime.now(timezone.utc)
        db.commit()
        return jsonify({"ok": True, "trashed": True})
    finally:
        db.close()


@app.route("/api/v1/briefs/<brief_id>/restore", methods=["POST"])
def restore_brief(brief_id: str):
    """Restaure un brief depuis la corbeille (clear ``trashed_at``)."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400

    db = SessionLocal()
    try:
        b = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.id == brief_id,
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.isnot(None),
            )
            .first()
        )
        if not b:
            return jsonify({"error": "not_in_trash"}), 404
        b.trashed_at = None
        db.commit()
        return jsonify({"ok": True, "restored": True})
    finally:
        db.close()


@app.route("/api/v1/briefs/<brief_id>/permanently", methods=["DELETE"])
def hard_delete_brief(brief_id: str):
    """Hard-delete d'un brief en corbeille (depuis purge ou bouton UI)."""
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or request.args.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400

    db = SessionLocal()
    try:
        b = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.id == brief_id,
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.isnot(None),
            )
            .first()
        )
        if not b:
            return jsonify({"error": "not_in_trash"}), 404
        db.delete(b)
        db.commit()
        return jsonify({"ok": True, "deleted": True})
    finally:
        db.close()


@app.route("/api/v1/briefs/purge", methods=["POST"])
def purge_briefs():
    """Hard-delete des briefs en corbeille depuis > N jours pour ``user_sub``.

    Appelé par code-generator depuis ``_purge_expired_trash`` pour étendre
    le balayage opportuniste à la zone interne.
    Body: ``{"user_sub": "...", "older_than_days": 30}``.
    Réponse: ``{"ok": True, "purged": <int>}``.
    """
    if not verify_token():
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    user_sub = (data.get("user_sub") or "").strip()
    if not user_sub:
        return jsonify({"error": "user_sub required"}), 400
    try:
        older_than_days = max(1, int(data.get("older_than_days") or 30))
    except (TypeError, ValueError):
        older_than_days = 30
    threshold = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    db = SessionLocal()
    try:
        rows = (
            db.query(MeetingBrief)
            .filter(
                MeetingBrief.user_sub == user_sub,
                MeetingBrief.trashed_at.isnot(None),
                MeetingBrief.trashed_at < threshold,
            )
            .all()
        )
        n = 0
        for b in rows:
            db.delete(b)
            n += 1
        if n:
            db.commit()
            logger.info("MeetingBrief purge: user=%s purged=%d", user_sub, n)
        return jsonify({"ok": True, "purged": n})
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
