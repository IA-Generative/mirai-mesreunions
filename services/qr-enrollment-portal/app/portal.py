"""QR Enrollment Portal — générateur de QR d'enrôlement derrière Passage2.

Service autonome : l'agent ouvre la page sur son ordinateur (authentifié par
Passage2), génère un QR code et le scanne avec l'app mobile ou la PWA
d'upload. Même parcours que l'onglet « Appareils » de mes-réunions, sans
dépendre de mesreunions-web ni de sa session OIDC.

Chaîne d'identité : en-têtes Passage2 → e-mail → compte Keycloak (``sub``)
→ device-token-authority. Les fichiers déposés arrivent ainsi dans le même
espace mes-réunions que ceux d'un QR généré depuis mes-réunions.

``create_app`` reçoit ses dépendances (config Passage2, résolveur de comptes,
fabrique de sessions DB, client de l'API interne) pour être testable ;
``app/main.py`` les construit depuis l'environnement.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

import requests as req
from flask import Flask, g, jsonify, render_template, request

from libs.shared.app.config import (
    CODE_TTL_MAX_MINUTES, CODE_TTL_MINUTES, MAX_UPLOADS_PER_SESSION,
)
from libs.shared.app.enrollment import (
    enrich_devices, make_qr_png, record_upload_session,
    request_token_from_internal, upload_url_for,
)
from libs.shared.app.keycloak_users import AmbiguousUserError, KeycloakLookupError
from libs.shared.app.models import UploadSession
from libs.shared.app.passage2 import Passage2Config, Passage2Error, read_identity
from libs.shared.app.rate_limit import SlidingWindowLimiter
from libs.shared.app.security import resolve_auto_transcribe

logger = logging.getLogger("qr_enrollment_portal")

# En-tête exigé sur toute requête qui modifie l'état : un site tiers ne peut
# pas l'ajouter sans pré-vol CORS (que le service n'autorise pas), ce qui
# bloque la CSRF via le cookie de session Passage2 du navigateur.
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "qr-enrollment-portal"

# Champs d'appareil exposés au navigateur : jamais ``device_key`` ni les
# empreintes, que device-token-authority renvoie pour l'usage interne.
DEVICE_PUBLIC_FIELDS = (
    "device_id", "device_name", "status", "confirmed_at", "revoked_at",
    "retention_expires_at", "last_seen_at", "created_at",
    "recent_uploads_24h", "remaining_uploads", "session_max_uploads",
    "session_upload_count", "session_simple_code", "session_expiring_soon",
    "session_expires_at",
)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def create_app(*, passage2: Passage2Config, resolve_user: Callable[[str], dict | None],
               session_factory, upload_base_url: str,
               internal_api: Callable[..., dict],
               issue_token: Callable[..., dict] = request_token_from_internal,
               generate_limiter: SlidingWindowLimiter | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    limiter = generate_limiter or SlidingWindowLimiter(max_events=10, window_seconds=60)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def _resolve_current_user() -> dict:
        identity = read_identity(passage2, request.headers, request.remote_addr)
        try:
            user = resolve_user(identity.email)
        except AmbiguousUserError:
            raise Passage2Error("ambiguous_account", 409,
                                "Plusieurs comptes mes-réunions portent cette adresse : "
                                "contactez le support.")
        except KeycloakLookupError:
            logger.exception("Keycloak lookup failed")
            raise Passage2Error("directory_unavailable", 503,
                                "Annuaire indisponible, réessayez dans un instant.")
        if not user:
            raise Passage2Error("no_account", 403,
                                f"Aucun compte mes-réunions n'est associé à {identity.email}. "
                                "Connectez-vous une première fois à mes-réunions.")
        # Le nom Passage2 (s'il est transmis) prime pour l'affichage.
        if identity.display_name and identity.display_name != identity.email:
            user = {**user, "name": identity.display_name}
        return user

    def require_user(api: bool):
        def decorator(view):
            @wraps(view)
            def wrapped(*args, **kwargs):
                if api and request.method != "GET" and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                    return jsonify({"error": "csrf", "message": "Requête refusée."}), 403
                try:
                    g.user = _resolve_current_user()
                except Passage2Error as err:
                    logger.info("Passage2 refusal: %s", err.code)
                    if api:
                        return jsonify({"error": err.code, "message": err.message}), err.status
                    return render_template("error.html", message=err.message), err.status
                return view(*args, **kwargs)
            return wrapped
        return decorator

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.route("/")
    @require_user(api=False)
    def index():
        return render_template("index.html", user=g.user,
                               ttl_minutes=CODE_TTL_MINUTES, max_ttl_minutes=CODE_TTL_MAX_MINUTES)

    @app.route("/api/me")
    @require_user(api=True)
    def api_me():
        return jsonify({"email": g.user.get("email"), "name": g.user.get("name")})

    @app.route("/api/generate-code", methods=["POST"])
    @require_user(api=True)
    def api_generate_code():
        if not limiter.allow(g.user["sub"], now=time.monotonic()):
            return jsonify({"error": "rate_limited",
                            "message": "Trop de QR générés, patientez une minute."}), 429
        data = request.get_json(silent=True) or {}
        try:
            ttl_minutes = min(max(int(data.get("ttl_minutes", CODE_TTL_MINUTES)), 1),
                              CODE_TTL_MAX_MINUTES)
            max_uploads = min(max(int(data.get("max_uploads", MAX_UPLOADS_PER_SESSION)), 1),
                              MAX_UPLOADS_PER_SESSION)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_params", "message": "Paramètres invalides."}), 400
        auto_transcribe = resolve_auto_transcribe(data.get("auto_transcribe", False))

        try:
            token_data = issue_token(g.user, ttl_minutes, max_uploads,
                                     auto_transcribe=auto_transcribe)
        except Exception:  # autorité interne injoignable ou en erreur
            logger.exception("Token issuance failed")
            return jsonify({"error": "issuer_unavailable",
                            "message": "Service de génération indisponible. Réessayez."}), 503

        db = session_factory()
        try:
            upload_session = record_upload_session(
                db, g.user, token_data, ttl_minutes=ttl_minutes,
                max_uploads=max_uploads, auto_transcribe=auto_transcribe,
            )
        finally:
            db.close()

        upload_url = upload_url_for(upload_base_url, token_data["qr_token"])
        qr_png = base64.b64encode(make_qr_png(upload_url)).decode()
        return jsonify({
            "session_id": str(upload_session.id),
            "simple_code": token_data["simple_code"],
            "qr_token": token_data["qr_token"],
            "upload_url": upload_url,
            "qr_image": f"data:image/png;base64,{qr_png}",
            "expires_at": token_data["expires_at"],
            "ttl_minutes": ttl_minutes,
            "max_uploads": max_uploads,
            "auto_transcribe": auto_transcribe,
        })

    @app.route("/api/my-devices")
    @require_user(api=True)
    def api_my_devices():
        sub = g.user["sub"]
        db = session_factory()
        try:
            devices = internal_api("GET", "/api/v1/devices", params={"user_sub": sub})
            sessions = db.query(UploadSession).filter(UploadSession.user_sub == sub).all()
            enriched = enrich_devices(devices if isinstance(devices, list) else [],
                                      sessions, now=datetime.now(timezone.utc))
            return jsonify([{k: d.get(k) for k in DEVICE_PUBLIC_FIELDS} for d in enriched])
        except Exception:
            logger.exception("Device listing failed")
            return jsonify({"error": "device_list_unavailable"}), 503
        finally:
            db.close()

    def _device_action(method: str, path: str, body: dict, error_code: str):
        try:
            data = internal_api(method, path, json_body={"user_sub": g.user["sub"], **body})
        except Exception:
            logger.exception("Device action failed: %s %s", method, path)
            return jsonify({"error": error_code}), 502
        return jsonify({"ok": True, **({"revoked": int(data.get("revoked", 0))}
                                        if isinstance(data, dict) and "revoked" in data else {})})

    @app.route("/api/my-devices/<device_id>/rename", methods=["POST"])
    @require_user(api=True)
    def api_rename_device(device_id):
        name = ((request.get_json(silent=True) or {}).get("device_name") or "").strip()[:80]
        if not name:
            return jsonify({"error": "device_name_required"}), 400
        return _device_action("POST", f"/api/v1/devices/{device_id}/rename",
                              {"device_name": name}, "device_rename_failed")

    @app.route("/api/my-devices/<device_id>/revoke", methods=["POST"])
    @require_user(api=True)
    def api_revoke_device(device_id):
        return _device_action("POST", f"/api/v1/devices/{device_id}/revoke",
                              {"reason": "revoked_from_qr_portal"}, "device_revoke_failed")

    @app.route("/api/my-devices/revoke-all", methods=["POST"])
    @require_user(api=True)
    def api_revoke_all():
        return _device_action("POST", "/api/v1/devices/revoke-all",
                              {"reason": "revoked_all_from_qr_portal"}, "device_revoke_all_failed")

    return app


def internal_device_api(method: str, path: str, *, json_body=None, params=None) -> dict:
    """Appel authentifié à device-token-authority (zone interne)."""
    from libs.shared.app.config import INTERNAL_API_TOKEN

    base = os.getenv("TOKEN_ISSUER_INTERNAL_BASE_URL", "http://device-token-authority:8091")
    resp = req.request(method, f"{base.rstrip('/')}{path}", json=json_body, params=params,
                       headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"}, timeout=10)
    resp.raise_for_status()
    return resp.json() if resp.content else {}
