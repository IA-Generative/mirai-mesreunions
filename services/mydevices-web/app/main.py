"""
mydevices-web — Code Generator Service (bootstrap minimal)
============================================================
Interface authentifiée (OIDC/Keycloak) pour générer des QR/codes simples
liant à l'mobile-upload-pwa.

PR3-v2 : ce fichier est désormais un shell de bootstrap. Tous les endpoints
métier sont sous ``app/modules/<name>/`` :

- ``app.modules.auth``         — OIDC routes (/login, /auth/callback, /logout)
- ``app.modules.devices``      — QR / enroll / retention / my-devices
- ``app.modules.sessions``     — uploads, fichiers, corbeille, transcript dl
- ``app.modules.preparations`` — /api/preparations/* (wizard LLM + Drive)
- ``app.modules.meetings``     — /api/meetings/* (CR + reprocess)
- ``app.modules.drive_sync``   — versement Drive best-effort (helper module)
- ``app.modules.glossary``     — extraction de glossaire (helper module)

L'init OIDC ``oauth.register(...)`` reste ici (exigence module-level de
flask-oauthlib). Les configs S3/RabbitMQ/OIDC sont publiées dans le mini
registre ``app.runtime`` que les blueprints consomment via getters.
"""

import logging
import os
import sys

from flask import Flask, redirect, render_template
from authlib.integrations.flask_client import OAuth

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import (
    ALLOWED_AUDIO_EXTENSIONS, DEVICE_TOKEN_RETENTION_HOURS,
    OIDCConfig, OIDC_OFFLINE_ACCESS, RabbitMQConfig, SECRET_KEY,
    UPLOAD_MAX_FILE_SIZE_MB, load_ext_db, load_s3_internal, load_s3_processed,
    load_s3_upload,
)
from libs.shared.app.models import ExternalBase
from libs.shared.app.database import create_session_factory, init_tables
from libs.shared.app.security import require_strong_shared_secret

from app import runtime
from app.shared import get_current_user, require_auth

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


# ─── Flask App ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024


# ─── Configs boot ───────────────────────────────────────────────────────

oidc_cfg = OIDCConfig()
db_cfg = load_ext_db()
s3_upload_cfg = load_s3_upload()
s3_processed_cfg = load_s3_processed()
s3_internal_cfg = load_s3_internal()
rabbit_cfg = RabbitMQConfig()

ALLOW_SHORT_QR_TTL_SECONDS_TEST = os.getenv("ALLOW_SHORT_QR_TTL_SECONDS_TEST", "").lower() in {"1", "true", "yes"}
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").strip()
NORMALIZATION_ANALYSIS_MAX_SECONDS = max(30, int(os.getenv("NORMALIZATION_ANALYSIS_MAX_SECONDS", "180")))


# ─── OIDC Setup (module-level — exigence flask-oauthlib) ────────────────

_OIDC_SCOPE_BASE = "openid email profile"
_OIDC_SCOPE = f"{_OIDC_SCOPE_BASE} offline_access" if OIDC_OFFLINE_ACCESS else _OIDC_SCOPE_BASE
oidc_internal_issuer = os.getenv("OIDC_INTERNAL_ISSUER", oidc_cfg.issuer).rstrip("/")

oauth = OAuth(app)
oauth.register(
    name="keycloak",
    client_id=oidc_cfg.client_id,
    client_secret=oidc_cfg.client_secret,
    server_metadata_url=f"{oidc_cfg.issuer}/.well-known/openid-configuration",
    client_kwargs={"scope": _OIDC_SCOPE},
)

if OIDC_OFFLINE_ACCESS:
    logger.info("OIDC offline_access scope ENABLED — refresh tokens will be persisted")
else:
    logger.info("OIDC offline_access scope DISABLED — set OIDC_OFFLINE_ACCESS=true to enable MCR push prerequisite")


# ─── Pages racine + meeting-prep ────────────────────────────────────────

@app.route("/healthz")
def healthz():
    from flask import jsonify
    return jsonify({"status": "ok", "service": "mydevices-web"}), 200


@app.route("/")
@require_auth
def index():
    user = get_current_user()
    return render_template(
        "index.html",
        user=user,
        short_ttl_enabled=ALLOW_SHORT_QR_TTL_SECONDS_TEST,
        device_retention_days=max(1, DEVICE_TOKEN_RETENTION_HOURS // 24),
        allowed_audio_extensions=",".join(ALLOWED_AUDIO_EXTENSIONS),
    )


@app.route("/meeting-prep")
@require_auth
def meeting_prep_page():
    """Deep-link historique → onglet « Préparation de réunion » de mydevices."""
    return redirect("/?tab=brief", code=302)


@app.route("/meeting-prep/new")
@require_auth
def meeting_prep_new_page():
    """Deep-link historique vers le wizard — désormais ouvert inline dans
    l'app (modale fullscreen). Cf. frontend/tabs/wizard.js qui détecte
    le query param ?action=new au boot pour l'auto-ouverture.
    """
    return redirect("/?tab=brief&action=new", code=302)


# Import lazy : helpers meeting_prep (utilisés par les blueprints).
from app import meeting_prep as _meeting_prep  # noqa: E402,F401


# ─── Init & Run ─────────────────────────────────────────────────────────


def create_app():
    require_strong_shared_secret("INTERNAL_API_TOKEN")
    init_tables(db_cfg, ExternalBase)
    session_factory = create_session_factory(db_cfg)

    runtime.configure_runtime(
        session_factory=session_factory,
        s3_upload_cfg=s3_upload_cfg,
        s3_processed_cfg=s3_processed_cfg,
        s3_internal_cfg=s3_internal_cfg,
        rabbit_cfg=rabbit_cfg,
        oidc_cfg=oidc_cfg,
        oidc_scope=_OIDC_SCOPE,
        oidc_internal_issuer=oidc_internal_issuer,
        allow_short_qr_ttl=ALLOW_SHORT_QR_TTL_SECONDS_TEST,
        public_host=PUBLIC_HOST,
        normalization_analysis_max_seconds=NORMALIZATION_ANALYSIS_MAX_SECONDS,
    )

    _register_modular_blueprints(app)
    return app


def _register_modular_blueprints(flask_app):
    """Attache les blueprints sous ``app/modules/``.

    Garde-fou : ne pas register deux fois (Flask le rejetterait au boot).
    """
    from app.modules.preparations import preparations_bp
    from app.modules.meetings import meetings_bp
    from app.modules.auth import auth_bp
    from app.modules.devices import devices_bp
    from app.modules.sessions import sessions_bp

    registered = {b.name for b in flask_app.blueprints.values()}
    for name, bp in (
        ("auth", auth_bp),
        ("devices", devices_bp),
        ("sessions", sessions_bp),
        ("preparations", preparations_bp),
        ("meetings", meetings_bp),
    ):
        if name not in registered:
            flask_app.register_blueprint(bp)


# WSGI entrypoint for Gunicorn
application = create_app()


if __name__ == "__main__":
    port_value = os.getenv("CODE_GENERATOR_BIND_PORT") or os.getenv("CODE_GENERATOR_PORT", "8080")
    if isinstance(port_value, str) and port_value.startswith("tcp://"):
        port_value = os.getenv("CODE_GENERATOR_BIND_PORT", "8080")
    port = int(port_value)
    application.run(host="0.0.0.0", port=port, debug=os.getenv("ENVIRONMENT") == "development")
