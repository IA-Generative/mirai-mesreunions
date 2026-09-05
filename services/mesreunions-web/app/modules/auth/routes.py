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
from libs.shared.app.oidc_auth import verify_id_token, OidcAuthError, is_user_admin  # noqa: E402

from app.runtime import (
    get_oidc_cfg, get_oidc_internal_issuer, get_oidc_scope,
)
from app.modules.auth import token_store

logger = logging.getLogger("mesreunions_web.auth.routes")

bp = Blueprint("auth", __name__)


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


def safe_next_target(raw: "str | None") -> "str | None":
    """Valide une destination de retour après connexion.

    N'accepte qu'un chemin interne : une URL absolue permettrait à un lien
    entrant de faire rebondir l'utilisateur vers un site tiers juste après
    son authentification (open redirect). ``//evil.tld`` et les schémas
    exotiques sont donc refusés au même titre que ``https://…``.
    """
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    if "\\" in candidate or "\n" in candidate or "\r" in candidate:
        return None
    return candidate[:500]


@bp.route("/login")
def login():
    oidc_cfg = get_oidc_cfg()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    session["oidc_state"] = state
    session["oidc_nonce"] = nonce
    # Mémorise la page demandée : sans ça, un lien entrant portant des
    # paramètres (cf. /preparer) perd tout son contexte quand l'utilisateur
    # n'était pas déjà connecté, et atterrit sur l'accueil.
    target = safe_next_target(request.args.get("next"))
    if target:
        session["post_login_next"] = target
    else:
        session.pop("post_login_next", None)
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
            # Garde anti-boucle : un seul retour automatique vers /login.
            # Sans elle, un cookie de session perdu côté navigateur
            # transformait ce redirect en boucle infinie /login ↔ Keycloak
            # (incident 2026-08-28).
            if session.pop("oidc_retry", None):
                logger.warning("OIDC login loop detected (invalid_grant twice) — stopping")
                return ("Connexion impossible : le fournisseur d'identité refuse le code "
                        "de connexion de façon répétée. Ferme cet onglet puis "
                        "<a href=\"/login\">réessaie</a> ; si le problème persiste, "
                        "vide les cookies du site.", 400)
            session["oidc_retry"] = True
            return redirect(url_for("auth.login"))
        return "Echec de connexion OIDC (code expiré ou déjà utilisé).", 400

    try:
        token = token_resp.json()
    except Exception:
        logger.warning("OIDC token response is not JSON: %s", (token_resp.text or "")[:300])
        return "Réponse OIDC invalide (token).", 502

    # Vérification cryptographique de l'id_token (signature JWKS + nonce) :
    # source d'identité autoritaire et fail-closed. Remplace l'ancien repli
    # sur un décodage non vérifié.
    expected_nonce = session.get("oidc_nonce", "")
    jwks_url = f"{oidc_internal_issuer}/protocol/openid-connect/certs"
    try:
        userinfo = verify_id_token(
            token.get("id_token", ""),
            audience=oidc_cfg.client_id,
            issuer={oidc_cfg.issuer.rstrip("/"), oidc_internal_issuer},
            jwks_url=jwks_url,
            nonce=expected_nonce,
        )
    except OidcAuthError:
        logger.warning("OIDC id_token verification failed", exc_info=True)
        session.pop("oidc_state", None)
        session.pop("oidc_nonce", None)
        return "Echec de vérification de l'identité OIDC.", 400

    # Enrichissement best-effort via userinfo (email/name). Non requis pour
    # la sécurité : l'id_token vérifié fait foi.
    try:
        userinfo_resp = _oidc_request_with_retry(
            "GET",
            f"{oidc_internal_issuer}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {token.get('access_token', '')}"},
            timeout=10,
        )
        if userinfo_resp.status_code < 400:
            enriched = userinfo_resp.json() or {}
            if isinstance(enriched, dict):
                userinfo = {**userinfo, **enriched}
    except Exception:
        logger.info("OIDC userinfo enrichment unavailable (verified id_token claims used)")

    # Droits admin = appartenance au groupe Keycloak (claim `groups`), calculée
    # UNE FOIS au login et stockée comme booléen compact. On NE stocke PAS la
    # liste `groups` en session : ces realms renvoient des dizaines de groupes
    # (+ id/access/refresh tokens déjà en session) → le cookie dépasserait la
    # limite ~4 Ko et l'ingress renverrait 502 (header trop gros).
    _admin_allowed = {x.strip() for x in os.getenv("ADMIN_ALLOWED_USERS", "").split(",") if x.strip()}
    _is_admin = is_user_admin(
        {
            "preferred_username": userinfo.get("preferred_username", ""),
            "email": userinfo.get("email", ""),
            "name": userinfo.get("name", ""),
            "sub": userinfo.get("sub", ""),
            "groups": userinfo.get("groups", []),
        },
        _admin_allowed,
    )
    session["user"] = {
        "sub": userinfo.get("sub", ""),
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", userinfo.get("preferred_username", "")),
        "is_admin": _is_admin,
    }
    # Les jetons (id/access/refresh) ne vont PLUS dans le cookie de session :
    # à trois JWT le cookie dépassait ~4093 octets et les navigateurs le
    # jetaient silencieusement → boucle de login infinie (incident
    # 2026-08-28). Ils vivent en base (web_session_tokens), le cookie ne
    # porte que la référence. L'access sert aux proxys serveur→serveur
    # (ex. video-ingest), le refresh au refresh silencieux (durée SSO
    # Session Idle Keycloak ≈ 30 min par défaut).
    ref = token_store.save_tokens(
        userinfo.get("sub", ""),
        id_token=token.get("id_token", ""),
        access_token=token.get("access_token", ""),
        refresh_token=token.get("refresh_token"),
    )
    if ref:
        session["token_ref"] = ref
    # Purge des clés héritées d'une session d'avant la migration (sinon un
    # vieux cookie resté gros continuerait de déclencher la limite).
    for legacy_key in ("id_token", "access_token", "refresh_token"):
        session.pop(legacy_key, None)
    session.pop("oidc_state", None)
    session.pop("oidc_nonce", None)
    session.pop("oidc_retry", None)

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

    target = safe_next_target(session.pop("post_login_next", None))
    return redirect(target or url_for("index"))


@bp.route("/logout")
def logout():
    oidc_cfg = get_oidc_cfg()
    # id_token en base (cookie → token_ref) ; repli sur la clé de session
    # héritée pour les cookies posés avant la migration.
    id_token_hint = token_store.load_tokens().get("id_token") or session.get("id_token")
    token_store.delete_tokens()
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
