"""
Helpers transverses mesreunions-web — utilisés par main.py ET par les blueprints
extraits sous ``app/modules/``. Mis à part pour éviter les imports circulaires :
les blueprints (preparations, meetings, …) importent depuis ``app.shared``
plutôt que depuis ``app.main`` (qui les enregistre).

Refacto PR3 : seul ce module ET ``db`` sont partagés horizontalement entre
modules. Tout le reste passe par fonctions publiques explicites.
"""

from __future__ import annotations

import logging
import os
from functools import wraps

import requests as req
from flask import redirect, request, session, url_for

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from libs.shared.app.config import INTERNAL_API_TOKEN  # noqa: E402

logger = logging.getLogger("mesreunions_web.shared")


def get_current_user():
    """Retourne le payload OIDC stocké en session, ou None."""
    user = session.get("user")
    if not user:
        return None
    return user


def require_auth(f):
    """Décorateur Flask : redirige vers /login si pas de session OIDC.

    La page demandée est transmise en ``?next=`` pour être restaurée après
    l'authentification : un lien entrant porteur de paramètres (cf. la route
    ``/preparer``, ouverte depuis une application tierce) perdait sinon tout
    son contexte dès lors que l'utilisateur n'était pas déjà connecté.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            target = request.full_path if request.query_string else request.path
            return redirect(url_for("auth.login", next=target))
        return f(*args, **kwargs)
    return decorated


def request_internal_device_api(
    method: str,
    path: str,
    *,
    json_body=None,
    timeout: int = 10,
    params=None,
) -> dict:
    """Appel HTTP vers la zone interne (``device-token-authority``).

    Authentifié via ``INTERNAL_API_TOKEN``. Lève ``requests.HTTPError`` en cas
    de réponse ≥ 400.
    """
    base = os.getenv(
        "TOKEN_ISSUER_INTERNAL_BASE_URL",
        "http://device-token-authority:8091",
    ).rstrip("/")
    resp = req.request(
        method,
        f"{base}{path}",
        json=json_body,
        params=params,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:
            err = {"error": resp.text[:200] or "internal_api_error"}
        raise req.HTTPError(str(err), response=resp)
    if not resp.text:
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def request_internal_preparation_api(
    method: str, path: str, **kwargs
) -> dict:
    """Alias sémantique : appel device-token-authority pour les préparations.

    Identique à ``request_internal_device_api`` mais nommé pour rendre le code
    appelant plus lisible (le module `preparations` parle de préparations,
    pas de devices).
    """
    return request_internal_device_api(method, path, **kwargs)


def request_internal_meeting_api(
    method: str, path: str, **kwargs
) -> dict:
    """Alias sémantique : appel device-token-authority pour les meetings."""
    return request_internal_device_api(method, path, **kwargs)


def trigger_audio_reprocess(user_sub: str, file_id: str, preparation_id) -> None:
    """Best-effort : relance la chaîne LLM côté internal-ingester.

    Utilisé après ``link-audio`` quand le lien préparation↔audio change, et
    par l'endpoint ``meetings/<id>/reprocess`` quand le glossaire est mis à
    jour. Aucune erreur ne remonte (UX best-effort).
    """
    base = os.getenv("FILE_PULLER_INTERNAL_BASE_URL") or ""
    if not base:
        logger.info("reprocess: FILE_PULLER_INTERNAL_BASE_URL not configured, skipping")
        return
    url = f"{base.rstrip('/')}/api/v1/audio/{file_id}/reprocess"
    try:
        req.post(
            url,
            json={
                "user_sub": user_sub,
                "glossary_from_preparation_id": preparation_id,
                "glossary_from_brief_id": preparation_id,  # alias legacy
            },
            headers={"Authorization": f"Bearer {INTERNAL_API_TOKEN}"},
            timeout=5,
        )
    except Exception:
        logger.exception("reprocess: internal-ingester call failed url=%s", url)
