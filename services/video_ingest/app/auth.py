"""Vérification JWT Bearer — autonome, sans dépendance à `libs.shared` (D14).

Modèle : le client (Mes Réunions, Mes Collections, agent MCP) présente
un `Authorization: Bearer <token>` issu de Keycloak. Le service vérifie
la signature contre le JWKS du realm, contrôle exp/iat/aud, et extrait
le `sub` qui devient l'identité opaque utilisée partout dans le service.

Variables d'env :
  - `VIDEO_INGEST_OIDC_JWKS_URL` : URL JWKS (ex. https://sso/.../jwks)
  - `VIDEO_INGEST_OIDC_AUDIENCE` : audience attendue (claim `aud`),
    optionnel. Si absent, on ne vérifie pas l'audience (utile en V1
    cross-clients).
  - `VIDEO_INGEST_AUTH_DISABLED` = "1" → bypass en DEV uniquement,
    logué WARN à chaque requête. Refusé si non explicitement activé.

Cache JWKS : in-memory, TTL 1h. Si la clé KID n'est pas trouvée, on
recharge une fois (rotation de clé Keycloak).
"""

from __future__ import annotations

import functools
import logging
import os
import time
import urllib.request

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from flask import g, jsonify, request

log = logging.getLogger(__name__)

_JWT = JsonWebToken(["RS256", "RS384", "RS512", "ES256", "ES384"])

_jwks_cache: dict = {"keys": None, "loaded_at": 0.0}
_JWKS_TTL = 3600.0


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _load_jwks(force: bool = False) -> dict:
    url = os.environ.get("VIDEO_INGEST_OIDC_JWKS_URL")
    if not url:
        raise AuthError("VIDEO_INGEST_OIDC_JWKS_URL non configuré", status=500)
    now = time.monotonic()
    if (not force and _jwks_cache["keys"] is not None
            and (now - _jwks_cache["loaded_at"]) < _JWKS_TTL):
        return _jwks_cache["keys"]
    with urllib.request.urlopen(url, timeout=5) as resp:
        import json as _json
        data = _json.loads(resp.read().decode("utf-8"))
    _jwks_cache["keys"] = JsonWebKey.import_key_set(data)
    _jwks_cache["loaded_at"] = now
    return _jwks_cache["keys"]


def verify_bearer(token: str) -> dict:
    """Renvoie les claims du token. Lève `AuthError` sinon."""
    if not token:
        raise AuthError("Token absent")
    try:
        keys = _load_jwks()
        claims = _JWT.decode(token, keys)
        # Force-reload JWKS once si le KID n'est pas reconnu (rotation Keycloak).
    except JoseError as e:
        msg = str(e).lower()
        if "kid" in msg or "key" in msg:
            try:
                keys = _load_jwks(force=True)
                claims = _JWT.decode(token, keys)
            except JoseError as e2:
                raise AuthError(f"JWT invalide : {e2}") from e2
        else:
            raise AuthError(f"JWT invalide : {e}") from e

    # Validation des claims standards.
    audience = os.environ.get("VIDEO_INGEST_OIDC_AUDIENCE")
    if audience:
        claims.options.setdefault("aud", {"essential": True, "value": audience})
    try:
        claims.validate()
    except JoseError as e:
        raise AuthError(f"JWT claims invalides : {e}") from e

    sub = claims.get("sub")
    if not sub:
        raise AuthError("JWT sans claim `sub`")
    return dict(claims)


def _extract_token() -> str:
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise AuthError("Header Authorization Bearer manquant")
    return h[len("Bearer "):].strip()


def require_auth(fn):
    """Décorateur Flask : valide le JWT et pose `g.user_sub` / `g.claims`.

    Bypass si `VIDEO_INGEST_AUTH_DISABLED=1` (DEV uniquement, log WARN).
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if os.environ.get("VIDEO_INGEST_AUTH_DISABLED") == "1":
            log.warning("AUTH DÉSACTIVÉE — VIDEO_INGEST_AUTH_DISABLED=1 (DEV uniquement)")
            g.user_sub = request.headers.get("X-Dev-User-Sub", "dev-anon")
            g.claims = {"sub": g.user_sub, "dev_bypass": True}
            return fn(*args, **kwargs)
        try:
            token = _extract_token()
            claims = verify_bearer(token)
        except AuthError as e:
            return jsonify({"error": str(e)}), e.status
        g.user_sub = claims["sub"]
        g.claims = claims
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Décorateur : au-dessus de `require_auth`, exige un rôle admin.

    V1 : on regarde le claim `realm_access.roles` (convention Keycloak)
    pour la présence de `video-ingest-admin` OU `admin`.
    """
    @functools.wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        roles = (g.claims or {}).get("realm_access", {}).get("roles", [])
        if "video-ingest-admin" not in roles and "admin" not in roles:
            return jsonify({"error": "Rôle admin requis"}), 403
        return fn(*args, **kwargs)
    return wrapper


def reset_cache_for_tests() -> None:
    _jwks_cache["keys"] = None
    _jwks_cache["loaded_at"] = 0.0
