"""
Client-side helper used by services that perform OIDC login (code-generator
and admin-portal) to persist a refresh token after a successful Authlib /
manual code-exchange flow.

Pattern : the calling service encrypts the refresh token with the shared
Fernet key (cf libs.shared.app.secrets_crypto) and POSTs the ciphertext to
the token-issuer's ``/api/v1/oidc-refresh-store`` endpoint, which performs
the UPSERT in postgres-internal. token-issuer never sees the plaintext
refresh token, so the Fernet key only needs to be present where encryption
or decryption actually happens (CG/admin write side, file-puller read side).

Why go through token-issuer instead of a direct DB write ?

  - code-generator and admin-portal connect to postgres-external by default
    (and to admin-int-db-secret in read-only mode for admin). Granting them
    direct write to ``oidc_refresh_tokens`` would expand their DB privileges
    in a way that costs more than this small HTTP indirection.
  - token-issuer is already the trusted authority writing other tables in
    postgres-internal (issued_tokens, device_enrollments). Adding one more
    write endpoint is consistent.

All errors are best-effort logged: a failed UPSERT degrades MCR push for
that user (they'll see ``mcr_auth_failed`` later) but **must not** break the
login flow itself — otherwise we'd lock users out of the portal.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests as req

from .secrets_crypto import encrypt

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 5


def _base_url() -> str:
    return os.getenv("TOKEN_ISSUER_INTERNAL_BASE_URL", "http://token-issuer:8091").rstrip("/")


def _store_url() -> str:
    return f"{_base_url()}/api/v1/oidc-refresh-store"


def _fetch_url(user_sub: str) -> str:
    return f"{_base_url()}/api/v1/oidc-refresh-fetch/{user_sub}"


def _delete_url(user_sub: str) -> str:
    return f"{_base_url()}/api/v1/oidc-refresh-delete/{user_sub}"


def _bearer() -> str:
    return os.getenv("INTERNAL_API_TOKEN", "")


def store_refresh_token(
    user_sub: str,
    refresh_token: Optional[str],
    keycloak_iss: Optional[str] = None,
    user_email: Optional[str] = None,
    bearer_token: str = "",
    timeout: int = _DEFAULT_TIMEOUT,
) -> bool:
    """
    Encrypt and persist a refresh token via token-issuer.

    Returns True if the UPSERT succeeded, False otherwise (and logs why).
    Never raises — callers should not fail the login over this.
    """
    if not user_sub:
        logger.warning("oidc_refresh_store: empty user_sub, skipping persistence")
        return False
    if not refresh_token:
        # Common: scope was openid+email+profile (no offline_access), or KC
        # refused the offline_access scope. Not an error here, just info.
        logger.info("oidc_refresh_store: no refresh_token in OIDC response, skipping")
        return False
    bearer = bearer_token or os.getenv("INTERNAL_API_TOKEN", "")
    if not bearer:
        logger.warning("oidc_refresh_store: INTERNAL_API_TOKEN not set, cannot call token-issuer")
        return False

    try:
        ciphertext = encrypt(refresh_token)
    except Exception:
        logger.exception("oidc_refresh_store: encryption failed for user_sub=%s", user_sub)
        return False

    payload = {
        "user_sub": user_sub,
        "ciphertext": ciphertext,
        "keycloak_iss": keycloak_iss or "",
        "user_email": user_email or "",
    }
    try:
        resp = req.post(
            _store_url(),
            json=payload,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        if resp.status_code >= 400:
            logger.warning(
                "oidc_refresh_store: token-issuer returned %s for user_sub=%s",
                resp.status_code, user_sub,
            )
            return False
        return True
    except req.RequestException:
        logger.exception("oidc_refresh_store: HTTP call to token-issuer failed")
        return False


def fetch_ciphertext(user_sub: str, timeout: int = _DEFAULT_TIMEOUT) -> Optional[str]:
    """
    Retrieve the stored ciphertext for ``user_sub``. Returns the Fernet
    ciphertext as a string, or None when the user has no stored token (404)
    or when the call fails.

    Used by file-puller at MCR push time.
    """
    if not user_sub:
        return None
    bearer = _bearer()
    if not bearer:
        logger.warning("fetch_ciphertext: INTERNAL_API_TOKEN not set")
        return None
    try:
        resp = req.get(
            _fetch_url(user_sub),
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=timeout,
        )
    except req.RequestException:
        logger.exception("fetch_ciphertext: HTTP call to token-issuer failed")
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        logger.warning("fetch_ciphertext: token-issuer returned %s for user_sub=%s",
                       resp.status_code, user_sub)
        return None
    try:
        return (resp.json() or {}).get("ciphertext")
    except Exception:
        logger.exception("fetch_ciphertext: response not JSON")
        return None


def delete_ciphertext(user_sub: str, timeout: int = _DEFAULT_TIMEOUT) -> bool:
    """
    Delete the stored ciphertext for ``user_sub``. Called when Keycloak
    rejects the refresh token (invalid_grant) so the next attempt fails
    fast instead of using a known-bad token.
    """
    if not user_sub:
        return False
    bearer = _bearer()
    if not bearer:
        logger.warning("delete_ciphertext: INTERNAL_API_TOKEN not set")
        return False
    try:
        resp = req.delete(
            _delete_url(user_sub),
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=timeout,
        )
        return resp.status_code < 400
    except req.RequestException:
        logger.exception("delete_ciphertext: HTTP call to token-issuer failed")
        return False
