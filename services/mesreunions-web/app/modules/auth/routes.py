"""Blueprint ``auth`` — OIDC routes ``/login``, ``/auth/callback``, ``/logout``.

PR3-v2 : extraction depuis ``main.py``. L'init OAuthLib (``oauth.register``)
reste dans ``main.py`` au module-level (l'inscription module-time est exigée
par flask-oauthlib). Le blueprint ici utilise les paramètres OIDC exposés
par ``app.runtime``.

⚠️ URLs préservées (``/login``, ``/auth/callback``, ``/logout``) :
- ``redirect_uri`` Keycloak du client ``mes-reunions`` pointe vers
  ``/auth/callback`` (cf mémoire ``feedback_oidc_redirect_uri_shared_secret``).
- ``offline_access`` scope hérité (cf ``feedback_oidc_offline_access_keycloak``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
import time
from urllib.parse import urlencode

import requests as req
from flask import Blueprint, redirect, request, session, url_for

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
from libs.shared.app.config import OIDC_OFFLINE_ACCESS  # noqa: E402
from libs.shared.app.oidc_refresh_store import store_refresh_token  # noqa: E402

from app.runtime import (
    get_oidc_cfg, get_oidc_internal_issuer, get_oidc_scope,
)

logger = logging.getLogger("mesreunions_web.auth.routes")

bp = Blueprint("auth", __name__)


def _decode_jwt_payload_unverified(token_value: str) -> dict:
    try:
        parts = token_value.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        pad = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _oidc_request_with_retry(method, url, *, max_attempts=3, retry_delay=0.7, **kwargs):
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return req.request(method, url, **kwargs)
        except req.RequestException as exc:
            last_exc = exc
            if attempt >= max_attempts:
                raise
            logger.warning("OIDC request failed (attempt %s/%s): %s", attempt, max_attempts, exc)
            time.sleep(retry_delay)
    if last_exc:
        raise last_exc
    raise RuntimeError("OIDC request failed unexpectedly")


@bp.route("/login")
def login():
    oidc_cfg = get_oidc_cfg()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    session["oidc_state"] = state
    session["oidc_nonce"] = nonce
    params = {
        "response_type": "code",
        "client_id": oidc_cfg.client_id,
        "redirect_uri": oidc_cfg.redirect_uri,
        "scope": get_oidc_scope(),
        "state": state,
        "nonce": nonce,
    }
    auth_url = f"{oidc_cfg.issuer.rstrip('/')}/protocol/openid-connect/auth?{urlencode(params)}"
    return redirect(auth_url)


@bp.route("/auth/callback")
def auth_callback():
    oidc_cfg = get_oidc_cfg()
    oidc_internal_issuer = get_oidc_internal_issuer() or oidc_cfg.issuer.rstrip("/")

    if session.get("user"):
        session.pop("oidc_state", None)
        session.pop("oidc_nonce", None)
        return redirect(url_for("index"))

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    if not code or not state or state != session.get("oidc_state"):
        return "OIDC callback invalide (state/code).", 400

    try:
        token_resp = _oidc_request_with_retry(
            "POST",
            f"{oidc_internal_issuer}/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": oidc_cfg.redirect_uri,
                "client_id": oidc_cfg.client_id,
                "client_secret": oidc_cfg.client_secret,
            },
            timeout=10,
        )
    except req.RequestException:
        logger.exception("OIDC token endpoint unreachable")
        return "OIDC indisponible (token endpoint). Réessaie.", 502

    if token_resp.status_code >= 400:
        body = (token_resp.text or "")[:500]
        logger.warning("OIDC token exchange failed: status=%s body=%s",
                       token_resp.status_code, body)
        if "invalid_grant" in body or "Code not valid" in body:
            session.pop("oidc_state", None)
            session.pop("oidc_nonce", None)
            return redirect(url_for("auth.login"))
        return "Echec de connexion OIDC (code expiré ou déjà utilisé).", 400

    try:
        token = token_resp.json()
    except Exception:
        logger.warning("OIDC token response is not JSON: %s", (token_resp.text or "")[:300])
        return "Réponse OIDC invalide (token).", 502

    try:
        userinfo_resp = _oidc_request_with_retry(
            "GET",
            f"{oidc_internal_issuer}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {token.get('access_token', '')}"},
            timeout=10,
        )
        if userinfo_resp.status_code >= 400:
            logger.warning("OIDC userinfo failed: status=%s body=%s",
                           userinfo_resp.status_code,
                           (userinfo_resp.text or "")[:500])
            userinfo = _decode_jwt_payload_unverified(token.get("id_token", ""))
            if not userinfo:
                return "Echec de récupération du profil OIDC.", 400
            logger.info("OIDC userinfo fallback to id_token claims")
        else:
            userinfo = userinfo_resp.json()
    except Exception:
        logger.exception("Failed to fetch userinfo from Keycloak")
        userinfo = _decode_jwt_payload_unverified(token.get("id_token", ""))
        if not userinfo:
            return "Erreur OIDC (userinfo). Réessaie.", 502
        logger.info("OIDC userinfo exception fallback to id_token claims")

    session["user"] = {
        "sub": userinfo.get("sub", ""),
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", userinfo.get("preferred_username", "")),
    }
    session["id_token"] = token.get("id_token", "")
    # Access token stocké pour les proxys serveur→serveur (ex. video-ingest).
    # Refresh token aussi (durée SSO Session Idle Keycloak ≈ 30 min par défaut)
    # pour permettre un refresh silencieux quand l'access expire pendant que
    # l'user est encore actif sur la page.
    session["access_token"] = token.get("access_token", "")
    if token.get("refresh_token"):
        session["refresh_token"] = token.get("refresh_token")
    session.pop("oidc_state", None)
    session.pop("oidc_nonce", None)

    if OIDC_OFFLINE_ACCESS:
        try:
            store_refresh_token(
                user_sub=userinfo.get("sub", ""),
                refresh_token=token.get("refresh_token"),
                keycloak_iss=oidc_cfg.issuer,
                user_email=userinfo.get("email", ""),
            )
        except Exception:
            logger.exception("Failed to persist OIDC refresh token (login still succeeded)")

    return redirect(url_for("index"))


@bp.route("/logout")
def logout():
    oidc_cfg = get_oidc_cfg()
    id_token_hint = session.get("id_token")
    session.clear()

    post_logout_redirect_uri = oidc_cfg.redirect_uri.replace("/auth/callback", "/")
    params = {
        "post_logout_redirect_uri": post_logout_redirect_uri,
        "client_id": oidc_cfg.client_id,
    }
    if id_token_hint:
        params["id_token_hint"] = id_token_hint
    logout_url = f"{oidc_cfg.issuer.rstrip('/')}/protocol/openid-connect/logout?{urlencode(params)}"
    return redirect(logout_url)
