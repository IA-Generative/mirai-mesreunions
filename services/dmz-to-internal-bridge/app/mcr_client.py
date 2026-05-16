"""
HTTP client for the MCR transcription platform.

Implements the three-step ingestion sequence documented in
``docs/integrate-with-mcr.md``:

  1. exchange refresh_token → access_token at the Keycloak token endpoint
  2. POST /meetings → meeting_id
  3. POST /meetings/{id}/presigned_url/generate → presigned PUT URL
  4. PUT binary on the presigned URL

Errors are classified into three families so the caller (internal-ingester) can
react appropriately:

  - ``MCRAuthError``        : refresh expired / invalid_grant. Caller must
                              wipe the stored refresh token and mark the
                              file as ``mcr_auth_failed`` — no retry.
  - ``MCRTransientError``   : 5xx, timeout, connection error. Caller should
                              raise so the queue retry counter handles it.
  - ``MCRApplicativeError`` : 4xx other than auth (bad payload, file too
                              large, unknown meeting). Caller marks
                              ``mcr_rejected`` — no retry.

All HTTP timeouts are conservative (10s default) so a stuck MCR side
doesn't hold the internal-ingester worker indefinitely. The PUT on the presigned
URL gets a longer timeout because the binary upload can be substantial.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests as req

logger = logging.getLogger(__name__)


# ─── Exceptions ────────────────────────────────────────────────

class MCRError(Exception):
    """Base class — never raised directly."""


class MCRAuthError(MCRError):
    """Refresh token expired or revoked. No retry, wipe the stored token."""


class MCRTransientError(MCRError):
    """5xx or network-level failure. Caller should let the queue retry."""


class MCRApplicativeError(MCRError):
    """4xx other than auth — bad payload, unknown meeting, etc. No retry."""


# ─── Client ────────────────────────────────────────────────────

class MCRClient:
    """
    Stateless wrapper around the MCR ingestion API. One instance per file
    is fine; there's no connection pooling state worth reusing across
    distinct user_subs.
    """

    def __init__(
        self,
        gateway_url: str,
        oidc_token_endpoint: str,
        oidc_client_id: str,
        oidc_client_secret: str = "",
        timeout: int = 10,
        upload_timeout: int = 120,
    ):
        if not gateway_url:
            raise ValueError("MCR_GATEWAY_URL is required to build MCRClient")
        if not oidc_token_endpoint:
            raise ValueError("OIDC_TOKEN_ENDPOINT is required to build MCRClient")
        self.gateway_url = gateway_url.rstrip("/")
        self.oidc_token_endpoint = oidc_token_endpoint
        self.oidc_client_id = oidc_client_id
        self.oidc_client_secret = oidc_client_secret
        self.timeout = timeout
        self.upload_timeout = upload_timeout

    # ── Step 1: refresh → access ─────────────────────────────

    def exchange_refresh(self, refresh_token: str) -> str:
        """Exchange a refresh token for a fresh access token."""
        if not refresh_token:
            raise MCRAuthError("Empty refresh token")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.oidc_client_id,
        }
        if self.oidc_client_secret:
            data["client_secret"] = self.oidc_client_secret
        try:
            resp = req.post(self.oidc_token_endpoint, data=data, timeout=self.timeout)
        except req.RequestException as exc:
            raise MCRTransientError(f"Keycloak token endpoint unreachable: {exc}") from exc
        if resp.status_code == 400:
            # Keycloak conventionally returns 400 invalid_grant when the refresh
            # has expired or been revoked. Treat any 400 from the token endpoint
            # as terminal authentication failure.
            body = (resp.text or "")[:300]
            raise MCRAuthError(f"Refresh exchange failed (400): {body}")
        if resp.status_code >= 500:
            raise MCRTransientError(f"Keycloak 5xx on token exchange: {resp.status_code}")
        if resp.status_code >= 400:
            raise MCRApplicativeError(f"Keycloak {resp.status_code} on token exchange: {(resp.text or '')[:200]}")
        try:
            access_token = resp.json().get("access_token", "")
        except Exception as exc:
            raise MCRTransientError(f"Keycloak response not JSON: {exc}") from exc
        if not access_token:
            raise MCRAuthError("Keycloak returned no access_token")
        return access_token

    # ── Step 2: create meeting ───────────────────────────────

    def create_meeting(self, access_token: str, meeting_payload: dict) -> str:
        """POST /meetings → meeting_id. ``meeting_payload`` is the full body."""
        url = f"{self.gateway_url}/meetings"
        try:
            resp = req.post(
                url,
                json=meeting_payload,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise MCRTransientError(f"MCR /meetings unreachable: {exc}") from exc
        self._raise_for_status(resp, context="POST /meetings")
        meeting_id = self._extract_field(resp, "meeting_id") or self._extract_field(resp, "id")
        if not meeting_id:
            raise MCRApplicativeError(f"MCR /meetings response missing meeting_id: {(resp.text or '')[:200]}")
        return str(meeting_id)

    # ── Step 3: presigned URL ────────────────────────────────

    def generate_presigned(self, access_token: str, meeting_id: str, filename: str) -> str:
        """POST /meetings/{id}/presigned_url/generate → presigned URL."""
        url = f"{self.gateway_url}/meetings/{meeting_id}/presigned_url/generate"
        try:
            resp = req.post(
                url,
                json={"filename": filename},
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise MCRTransientError(f"MCR presigned_url/generate unreachable: {exc}") from exc
        self._raise_for_status(resp, context="POST /presigned_url/generate")
        presigned = (
            self._extract_field(resp, "presigned_url")
            or self._extract_field(resp, "url")
            or self._extract_field(resp, "presignedUrl")
        )
        if not presigned:
            raise MCRApplicativeError(f"MCR presigned response missing url field: {(resp.text or '')[:200]}")
        return presigned

    # ── Step 4: PUT binary ───────────────────────────────────

    def upload_binary(self, presigned_url: str, body: bytes, content_type: str) -> None:
        """PUT the audio binary on the presigned URL."""
        try:
            resp = req.put(
                presigned_url,
                data=body,
                headers={"Content-Type": content_type},
                timeout=self.upload_timeout,
            )
        except req.RequestException as exc:
            raise MCRTransientError(f"Presigned PUT unreachable: {exc}") from exc
        if resp.status_code == 403 or resp.status_code == 401:
            # Presigned URLs that are signed with KC-issued credentials can
            # 403 if expired between generation and use.
            raise MCRApplicativeError(f"Presigned PUT auth failed: {resp.status_code}")
        if resp.status_code >= 500:
            raise MCRTransientError(f"Presigned PUT 5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise MCRApplicativeError(f"Presigned PUT {resp.status_code}: {(resp.text or '')[:200]}")

    # ── Internals ───────────────────────────────────────────

    @staticmethod
    def _raise_for_status(resp, context: str) -> None:
        if resp.status_code in (401, 403):
            raise MCRAuthError(f"{context} → {resp.status_code} (token rejected by MCR)")
        if resp.status_code >= 500:
            raise MCRTransientError(f"{context} → {resp.status_code}")
        if resp.status_code >= 400:
            raise MCRApplicativeError(f"{context} → {resp.status_code}: {(resp.text or '')[:200]}")

    @staticmethod
    def _extract_field(resp, key: str) -> Optional[str]:
        try:
            return resp.json().get(key)
        except Exception:
            return None
