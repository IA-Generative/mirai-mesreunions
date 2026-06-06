"""Brique d'authentification OIDC partagée — vérification homogène, fail-closed.

Centralise la vérification JWT/OIDC pour l'ensemble des services (web,
admin-console, …) en généralisant la brique JWKS éprouvée du connecteur
vidéo. Objectif : une seule surface à auditer, pas de dérive d'un service à
l'autre, fail-closed par construction.

Fonctions exposées :

  ``verify_oidc_token`` — vérifie un access/bearer JWT : signature (allowlist
  RS*/ES*, refus ``alg:none``), ``iss``/``exp``/``nbf`` et **audience
  obligatoire** (pas de confusion d'audience possible).

  ``verify_id_token`` — vérifie un id_token OIDC : même vérification de
  signature/claims + **contrôle du nonce** (anti-rejeu). Remplace tout repli
  sur un décodage non vérifié.

  ``assert_auth_startup_config`` — garde de démarrage fail-closed : refuse le
  boot si un drapeau de désactivation d'auth est positionné en contexte de
  production (le bypass reste autorisé en dev/test) ; supporte
  ``AUTH_MODE=gateway`` (délégation d'identité à un upstream vérifié, qui
  exige un secret de confiance — fail-closed sinon).

  ``is_user_admin`` / ``assert_admin_allowlist_configured`` — autorisation
  admin fail-closed : une liste d'accès vide interdit l'accès.

Cache JWKS : in-memory, TTL configurable (défaut 1h), rechargé une fois si un
KID n'est pas reconnu (rotation Keycloak).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Iterable, Optional

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

from .security import is_strong_shared_secret

log = logging.getLogger(__name__)

# Allowlist explicite : signatures asymétriques uniquement. ``none`` et les
# algos symétriques (HS*) sont volontairement exclus — un attaquant ne doit
# jamais pouvoir downgrader la vérification.
_ALLOWED_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
_JWT = JsonWebToken(_ALLOWED_ALGS)

_JWKS_TTL = 3600.0
_jwks_cache: dict = {}

# Marqueurs d'environnement considérés comme "production" pour la garde de
# démarrage. Tout le reste (development, test, local, vide) = non-prod.
_PROD_MARKERS = {"production", "prod", "prod-beta", "prodbeta", "preprod", "staging"}

_TRUTHY = {"1", "true", "yes", "on"}


class OidcAuthError(Exception):
    """Échec de vérification d'un token (signature, claims, nonce, audience)."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


class AuthStartupError(RuntimeError):
    """Configuration d'authentification non sûre détectée au démarrage."""


# ─── JWKS ──────────────────────────────────────────────────────────────────

def fetch_jwks(jwks_url: str, timeout: int = 5):
    """Charge un JWKS depuis son URL. Isolé pour être mocké en test."""
    with urllib.request.urlopen(jwks_url, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return JsonWebKey.import_key_set(data)


def _get_keys(jwks_url: str, *, force: bool = False):
    if not jwks_url:
        raise OidcAuthError("JWKS URL non configurée", status=500)
    now = time.monotonic()
    entry = _jwks_cache.get(jwks_url)
    if (not force and entry is not None
            and (now - entry["loaded_at"]) < _JWKS_TTL):
        return entry["keys"]
    keys = fetch_jwks(jwks_url)
    _jwks_cache[jwks_url] = {"keys": keys, "loaded_at": now}
    return keys


def reset_cache_for_tests() -> None:
    _jwks_cache.clear()


# ─── Vérification de token ──────────────────────────────────────────────────

def _decode_with_rotation(token: str, jwks_url: str):
    """Décode + vérifie la signature, rechargeant le JWKS une fois si le KID
    n'est pas reconnu (rotation de clé côté Keycloak)."""
    try:
        keys = _get_keys(jwks_url)
        return _JWT.decode(token, keys)
    except JoseError as exc:
        msg = str(exc).lower()
        if "kid" in msg or "key" in msg:
            try:
                keys = _get_keys(jwks_url, force=True)
                return _JWT.decode(token, keys)
            except JoseError as exc2:
                raise OidcAuthError(f"JWT invalide : {exc2}") from exc2
        raise OidcAuthError(f"JWT invalide : {exc}") from exc


def verify_oidc_token(
    token: str,
    *,
    audience: Optional[str],
    issuer: Optional[str],
    jwks_url: str,
) -> dict:
    """Vérifie un access/bearer JWT et renvoie ses claims.

    ``audience`` est **obligatoire** : un appel sans audience attendue lève
    ``OidcAuthError`` (fail-closed) afin d'interdire la confusion d'audience
    entre clients d'un même realm. ``issuer`` est fortement recommandé.
    """
    if not token:
        raise OidcAuthError("Token absent")
    if not audience:
        # Fail-closed : l'audience attendue doit toujours être fournie.
        raise OidcAuthError(
            "Audience attendue non configurée — vérification refusée "
            "(fail-closed contre la confusion d'audience)",
            status=500,
        )

    claims = _decode_with_rotation(token, jwks_url)
    claims.options["aud"] = {"essential": True, "value": audience}
    try:
        claims.validate()
    except JoseError as exc:
        raise OidcAuthError(f"JWT claims invalides : {exc}") from exc

    # Validation de l'issuer : accepte une valeur unique ou un ensemble
    # (utile quand Keycloak frappe ``iss`` différemment selon l'horizon
    # public/interne du déploiement).
    if issuer:
        expected = {issuer} if isinstance(issuer, str) else {str(i) for i in issuer}
        if claims.get("iss") not in expected:
            raise OidcAuthError("Issuer du JWT non reconnu")

    sub = claims.get("sub")
    if not sub:
        raise OidcAuthError("JWT sans claim `sub`")
    return dict(claims)


def verify_id_token(
    id_token: str,
    *,
    audience: Optional[str],
    issuer: Optional[str],
    jwks_url: str,
    nonce: Optional[str],
) -> dict:
    """Vérifie un id_token OIDC : signature + claims + **nonce**.

    Le ``nonce`` attendu (celui stocké en session au ``/login``) doit être
    fourni et correspondre au claim ``nonce`` du token. Un nonce attendu vide
    ou un claim absent/différent ⇒ refus (anti-rejeu — ANSSI PA-080 R26/R28).
    """
    if not nonce:
        # Pas de nonce en session : on ne peut pas garantir l'anti-rejeu.
        raise OidcAuthError("Nonce attendu absent — id_token refusé")

    claims = verify_oidc_token(
        id_token, audience=audience, issuer=issuer, jwks_url=jwks_url
    )
    token_nonce = claims.get("nonce")
    if not token_nonce or token_nonce != nonce:
        raise OidcAuthError("Nonce de l'id_token invalide")
    return claims


# ─── Garde de démarrage ─────────────────────────────────────────────────────

def is_production(env: Optional[dict] = None) -> bool:
    import os
    env = env if env is not None else os.environ
    marker = (env.get("ENVIRONMENT") or env.get("APP_ENV") or "").strip().lower()
    return marker in _PROD_MARKERS


def _is_truthy(value: str) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def assert_auth_startup_config(
    env: Optional[dict] = None,
    *,
    service_name: str = "service",
    auth_disable_flags: Iterable[str] = (),
) -> None:
    """Refuse un démarrage non sûr (fail-closed).

    - Tout drapeau de ``auth_disable_flags`` positionné en **production** ⇒
      ``AuthStartupError`` (le bypass reste autorisé en dev/test).
    - ``AUTH_MODE=gateway`` exige un ``AUTH_GATEWAY_SHARED_SECRET`` fort
      (confiance d'une identité upstream vérifiée — fail-closed sinon). Tout
      autre ``AUTH_MODE`` non reconnu est refusé.
    """
    import os
    env = env if env is not None else os.environ
    prod = is_production(env)

    if prod:
        for flag in auth_disable_flags:
            if _is_truthy(env.get(flag, "")):
                raise AuthStartupError(
                    f"[{service_name}] {flag} est positionné en production : "
                    "désactivation d'authentification interdite hors dev/test."
                )

    auth_mode = (env.get("AUTH_MODE") or "").strip().lower()
    if auth_mode in ("", "oidc"):
        return
    if auth_mode == "gateway":
        secret = env.get("AUTH_GATEWAY_SHARED_SECRET", "")
        if not is_strong_shared_secret(secret):
            raise AuthStartupError(
                f"[{service_name}] AUTH_MODE=gateway exige un "
                "AUTH_GATEWAY_SHARED_SECRET fort (confiance upstream vérifiée)."
            )
        return
    raise AuthStartupError(
        f"[{service_name}] AUTH_MODE={auth_mode!r} inconnu — valeurs admises : "
        "oidc, gateway."
    )


# ─── Autorisation admin (fail-closed) ───────────────────────────────────────

# Groupe Keycloak portant les droits d'administration. L'appartenance est la
# source de vérité des droits admin (claim `groups` de l'OIDC). Surchargable
# par env ``ADMIN_GROUP``.
DEFAULT_ADMIN_GROUP = "/g/admins"


def _admin_group(admin_group: Optional[str]) -> str:
    import os
    if admin_group is not None:
        return admin_group
    return os.getenv("ADMIN_GROUP", DEFAULT_ADMIN_GROUP)


def _normalize_group(value: str) -> str:
    """Normalise un chemin de groupe pour comparaison robuste (casse + slashes)."""
    return "/" + str(value or "").strip().strip("/").lower()


def user_groups(user: dict) -> list:
    """Liste des groupes portés par le claim ``groups`` (str ou liste)."""
    raw = (user or {}).get("groups")
    if isinstance(raw, str):
        raw = [raw]
    return [g for g in (raw or []) if g]


def is_user_admin(
    user: dict,
    allowed_users: Optional[Iterable[str]] = None,
    *,
    admin_group: Optional[str] = None,
) -> bool:
    """True si ``user`` a les droits d'administration. Fail-closed.

    Source de vérité : **appartenance au groupe** ``admin_group`` (claim
    ``groups`` OIDC), défaut ``/g/admins``. Une liste d'accès
    ``allowed_users`` optionnelle sert de secours (break-glass) ; vide/absente
    elle n'accorde rien. Aucun membre du groupe + hors allowlist ⇒ refus.
    """
    group = _admin_group(admin_group)
    if group:
        target = _normalize_group(group)
        if any(_normalize_group(g) == target for g in user_groups(user)):
            return True
    allowed = {str(x).strip().lower() for x in (allowed_users or []) if str(x).strip()}
    if not allowed:
        return False
    candidates = {
        str((user or {}).get("preferred_username", "")).lower(),
        str((user or {}).get("email", "")).lower(),
        str((user or {}).get("name", "")).lower(),
        str((user or {}).get("sub", "")).lower(),
    }
    return any(c and c in allowed for c in candidates)


def assert_admin_access_configured(
    allowed_users: Optional[Iterable[str]] = None,
    *,
    admin_group: Optional[str] = None,
) -> None:
    """Refuse le démarrage si **aucun** mécanisme d'admin n'est configuré
    (ni groupe, ni liste d'accès) — fail-closed."""
    group = _admin_group(admin_group)
    allowed = {str(x).strip().lower() for x in (allowed_users or []) if str(x).strip()}
    if not group and not allowed:
        raise AuthStartupError(
            "Aucun mécanisme d'autorisation admin configuré (ni ADMIN_GROUP ni "
            "ADMIN_ALLOWED_USERS) : refus de démarrer (fail-closed)."
        )


def assert_admin_allowlist_configured(allowed_users: Optional[Iterable[str]]) -> None:
    """Refuse le démarrage si la liste d'accès admin est vide (fail-closed).

    Conservé pour compatibilité ; préférer ``assert_admin_access_configured``
    (qui accepte une admin par groupe Keycloak)."""
    allowed = {str(x).strip().lower() for x in (allowed_users or []) if str(x).strip()}
    if not allowed:
        raise AuthStartupError(
            "Liste d'accès admin vide : la console refuse de démarrer "
            "(fail-closed). Renseigne ADMIN_ALLOWED_USERS."
        )
