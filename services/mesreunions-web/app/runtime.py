"""Registre runtime partagé pour mesreunions-web (PR3-v2).

Mis à part pour éviter les imports circulaires entre ``main.py`` et les
blueprints sous ``app/modules/``. Tous les états initialisés au boot
(session factory DB, configs S3, RabbitMQ, OIDC) sont stockés ici et
accessibles via des getters lazy.

Le boot est en deux temps :
1. ``main.py`` charge les configs depuis ``libs.shared.app.config`` et appelle
   ``configure_runtime(...)`` une seule fois au démarrage.
2. Les modules sous ``app/modules/`` lisent via ``get_session_factory()``,
   ``get_s3_upload_cfg()``, etc. — jamais via import direct depuis ``main``.
"""

from __future__ import annotations

from typing import Any, Optional

_state: dict[str, Any] = {
    "session_factory": None,
    "s3_upload_cfg": None,
    "s3_processed_cfg": None,
    "s3_internal_cfg": None,
    "rabbit_cfg": None,
    "oidc_cfg": None,
    "oidc_scope": None,
    "oidc_internal_issuer": None,
    "allow_short_qr_ttl": False,
    "public_host": "",
    "normalization_analysis_max_seconds": 180,
}


def configure_runtime(**kwargs: Any) -> None:
    """À appeler une fois depuis ``main.create_app()``."""
    _state.update(kwargs)


def get_session_factory():
    sf = _state.get("session_factory")
    if sf is None:
        raise RuntimeError("runtime not configured: session_factory missing")
    return sf


def session_scope():
    """Retourne une nouvelle session SQLAlchemy (caller responsable du close)."""
    return get_session_factory()()


def get_s3_upload_cfg():
    return _state["s3_upload_cfg"]


def get_s3_processed_cfg():
    return _state["s3_processed_cfg"]


def get_s3_internal_cfg():
    return _state["s3_internal_cfg"]


def get_rabbit_cfg():
    return _state["rabbit_cfg"]


def get_oidc_cfg():
    return _state["oidc_cfg"]


def get_oidc_scope() -> str:
    return _state.get("oidc_scope") or "openid email profile"


def get_oidc_internal_issuer() -> str:
    return _state.get("oidc_internal_issuer") or ""


def allow_short_qr_ttl() -> bool:
    return bool(_state.get("allow_short_qr_ttl"))


def get_public_host() -> str:
    return _state.get("public_host") or ""


def get_normalization_analysis_max_seconds() -> int:
    return int(_state.get("normalization_analysis_max_seconds") or 180)
