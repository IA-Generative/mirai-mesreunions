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
from libs.shared.app.models import InternalBase, IssuedToken
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret, verify_bearer_token

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)

db_cfg = load_int_db()
SessionLocal = None
ALLOW_SHORT_QR_TTL_SECONDS_TEST = os.getenv("ALLOW_SHORT_QR_TTL_SECONDS_TEST", "").lower() in {"1", "true", "yes"}


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


# ─── Init ───────────────────────────────────────────────────

def create_app():
    global SessionLocal
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, InternalBase)
    SessionLocal = create_session_factory(db_cfg)
    return app


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port = int(os.getenv("TOKEN_ISSUER_PORT", 8091))
    application.run(host="0.0.0.0", port=port)
