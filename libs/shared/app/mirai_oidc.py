"""
Shared Mirai OIDC token-exchange helper.

Exchange a stored offline refresh_token (captured at user login via the
``mes-reunions`` client) into a short-lived access_token usable against any
Mirai-realm protected API (MCR push gateway, MCR pull gateway, etc.).

This module factors out the logic that lived in
``services/dmz-to-internal-bridge/app/mcr_client.py`` so that
``mesreunions-web`` (sync calls from a user request) and
``internal-ingester`` (async worker) can share the exact same exchange
behavior + error taxonomy.

Error taxonomy:

  - ``OIDCAuthError``       : refresh expired / invalid_grant. Caller must
                              wipe the stored refresh token — no retry.
  - ``OIDCTransientError``  : 5xx, timeout, connection error. Caller should
                              raise so the queue retry counter handles it.
  - ``OIDCApplicativeError``: 4xx other than auth (bad client, etc.). No retry.
"""

from __future__ import annotations

import logging

import requests as req

logger = logging.getLogger(__name__)


class OIDCError(Exception):
    """Base class — never raised directly."""


class OIDCAuthError(OIDCError):
    """Refresh token expired or revoked. No retry, wipe the stored token."""


class OIDCTransientError(OIDCError):
    """5xx or network-level failure. Caller should let the queue retry."""


class OIDCApplicativeError(OIDCError):
    """4xx other than auth — bad client config, etc. No retry."""


def exchange_refresh_token(
    *,
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    client_secret: str = "",
    timeout: int = 10,
) -> str:
    """Exchange a refresh token for a fresh access token at a Keycloak token endpoint.

    Returns the ``access_token`` string. Raises one of the three error
    families above according to the failure mode.
    """
    if not token_endpoint:
        raise ValueError("token_endpoint is required")
    if not client_id:
        raise ValueError("client_id is required")
    if not refresh_token:
        raise OIDCAuthError("Empty refresh token")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        resp = req.post(token_endpoint, data=data, timeout=timeout)
    except req.RequestException as exc:
        raise OIDCTransientError(f"Keycloak token endpoint unreachable: {exc}") from exc
    if resp.status_code == 400:
        # Keycloak conventionally returns 400 invalid_grant when the refresh
        # has expired or been revoked. Treat any 400 from the token endpoint
        # as terminal authentication failure.
        body = (resp.text or "")[:300]
        raise OIDCAuthError(f"Refresh exchange failed (400): {body}")
    if resp.status_code >= 500:
        raise OIDCTransientError(f"Keycloak 5xx on token exchange: {resp.status_code}")
    if resp.status_code >= 400:
        raise OIDCApplicativeError(
            f"Keycloak {resp.status_code} on token exchange: {(resp.text or '')[:200]}"
        )
    try:
        access_token = resp.json().get("access_token", "")
    except Exception as exc:
        raise OIDCTransientError(f"Keycloak response not JSON: {exc}") from exc
    if not access_token:
        raise OIDCAuthError("Keycloak returned no access_token")
    return access_token
