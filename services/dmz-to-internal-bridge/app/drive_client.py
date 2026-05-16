"""
HTTP client for the suitenumerique/drive service (mesfichiers).

The Drive exposes a Django REST Framework API under ``/api/v1.0/items/...``
behind ``mozilla-django-oidc`` ``OIDCAuthentication``. We reuse the same
trust chain as the MCR push:

  1. exchange refresh_token → access_token at the Keycloak token endpoint
     (assumes Drive and this service share a Keycloak realm — both live in
     the suite numérique ecosystem).
  2. GET /api/v1.0/items/{id}/         → metadata of a single item
  3. GET /api/v1.0/items/{id}/children/ → list folder contents
  4. GET <download_url>                → fetch the binary payload

The download URL comes either from the metadata response (typical DRF
pattern with S3-backed FileField producing a presigned URL) or from a
conventional ``/items/{id}/download/`` endpoint. The client supports both:
``download_item`` first reads the metadata, looks for a known URL field,
and falls back to the ``/download/`` route if none is present.

Errors are classified into the same three families as ``MCRClient`` so the
caller (mobile-upload-pwa /api/meeting-prep route) can react uniformly:

  - ``DriveAuthError``        : refresh expired/revoked, or 401/403 from
                                Drive. Caller must wipe the refresh token
                                and tell the user to re-login. No retry.
  - ``DriveTransientError``   : 5xx, timeout, connection error. Caller
                                may retry once or fail soft.
  - ``DriveApplicativeError`` : 4xx other than auth (404 item not found,
                                403 not yours, bad payload). No retry.

Timeouts are conservative (10 s metadata, 60 s download) so a stuck Drive
doesn't hold the synchronous prep route indefinitely.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests as req

logger = logging.getLogger(__name__)


# ─── Exceptions ────────────────────────────────────────────────

class DriveError(Exception):
    """Base class — never raised directly."""


class DriveAuthError(DriveError):
    """Refresh expired/revoked, or 401/403 from Drive. No retry.

    ``status_code`` distingue les causes : 401 = la session/access token
    n'est pas valide (re-login utile) ; 403 = le token est valide mais
    l'utilisateur n'a pas accès à cette ressource précise (re-login
    inutile, c'est une question de permission Drive sur le folder/item).
    None pour les erreurs côté Keycloak (refresh expiré, etc.).
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class DriveTransientError(DriveError):
    """5xx or network failure. Caller may retry."""


class DriveApplicativeError(DriveError):
    """4xx other than auth — not found, forbidden, bad payload. No retry."""


# ─── Client ────────────────────────────────────────────────────

# Candidate field names on the item metadata that may carry a download URL.
# Tried in order until one yields a non-empty string.
#
# Ordre crucial : `url_permalink` d'abord car c'est le seul champ qui pointe
# systématiquement sur l'API DRF (`/api/v1.0/items/<id>/download/`) qui
# accepte le bearer token mozilla-django-oidc. Le champ `url` (que mesfichiers
# remplit sur tous les items) pointe sur `/media/item/<id>/<filename>`, route
# servie par un middleware d'auth différent (cookie session ou JWT media-auth)
# qui refuse notre bearer → 403 sur le download alors que le listing passe.
# Cf diagnostic 2026-05-14 (test-drive children_probe = 200 mais POST = 403).
_DOWNLOAD_URL_KEYS = ("url_permalink", "download_url", "presigned_url", "url", "file", "media_url")

# DRF base path. Items are the unified resource (folders are items too).
_API_PREFIX = "/api/v1.0"


class DriveClient:
    """
    Stateless wrapper around the Drive API. One instance per request is
    fine — there's no state worth reusing across users.
    """

    def __init__(
        self,
        base_url: str,
        oidc_token_endpoint: str,
        oidc_client_id: str,
        oidc_client_secret: str = "",
        timeout: int = 10,
        download_timeout: int = 60,
    ):
        if not base_url:
            raise ValueError("DRIVE_BASE_URL is required to build DriveClient")
        if not oidc_token_endpoint:
            raise ValueError("OIDC_TOKEN_ENDPOINT is required to build DriveClient")
        self.base_url = base_url.rstrip("/")
        self.oidc_token_endpoint = oidc_token_endpoint
        self.oidc_client_id = oidc_client_id
        self.oidc_client_secret = oidc_client_secret
        self.timeout = timeout
        self.download_timeout = download_timeout

    # ── Step 1: refresh → access ─────────────────────────────

    def exchange_refresh(self, refresh_token: str) -> str:
        """Exchange a refresh token for a fresh access token. Same flow as MCRClient."""
        if not refresh_token:
            raise DriveAuthError("Empty refresh token")
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
            raise DriveTransientError(f"Keycloak token endpoint unreachable: {exc}") from exc
        if resp.status_code == 400:
            body = (resp.text or "")[:300]
            raise DriveAuthError(f"Refresh exchange failed (400): {body}")
        if resp.status_code >= 500:
            raise DriveTransientError(f"Keycloak 5xx on token exchange: {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"Keycloak {resp.status_code} on token exchange: {(resp.text or '')[:200]}")
        try:
            access_token = resp.json().get("access_token", "")
        except Exception as exc:
            raise DriveTransientError(f"Keycloak response not JSON: {exc}") from exc
        if not access_token:
            raise DriveAuthError("Keycloak returned no access_token")
        return access_token

    # ── Step 2: metadata ─────────────────────────────────────

    def get_item(self, access_token: str, item_id: str) -> dict:
        """GET /api/v1.0/items/{id}/ → item metadata dict."""
        url = f"{self.base_url}{_API_PREFIX}/items/{item_id}/"
        try:
            resp = req.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise DriveTransientError(f"Drive GET item unreachable: {exc}") from exc
        self._raise_for_status(resp, context=f"GET items/{item_id}")
        try:
            data = resp.json()
        except Exception as exc:
            raise DriveTransientError(f"Drive item response not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise DriveApplicativeError(f"Drive item response is not an object: {type(data).__name__}")
        return data

    # ── Step 3: list children ────────────────────────────────

    def list_children(self, access_token: str, parent_id: str) -> list[dict]:
        """
        GET /api/v1.0/items/{id}/children/ → list of items inside a folder.

        Handles the two common DRF list shapes: a plain ``[...]`` array or a
        paginated ``{"results": [...]}`` envelope. Pagination beyond the first
        page is not followed — folder previews stop at the first ~50-100
        entries which is more than enough for a meeting prep UI.
        """
        url = f"{self.base_url}{_API_PREFIX}/items/{parent_id}/children/"
        try:
            resp = req.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise DriveTransientError(f"Drive list children unreachable: {exc}") from exc
        self._raise_for_status(resp, context=f"GET items/{parent_id}/children")
        try:
            data = resp.json()
        except Exception as exc:
            raise DriveTransientError(f"Drive children response not JSON: {exc}") from exc
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if not isinstance(data, list):
            raise DriveApplicativeError(
                f"Drive children response is neither array nor paginated envelope: {type(data).__name__}"
            )
        return [item for item in data if isinstance(item, dict)]

    # ── Step 4: download binary ──────────────────────────────

    def download_item(self, access_token: str, item_id: str, max_bytes: Optional[int] = None) -> tuple[bytes, str]:
        """
        Fetch the binary content of an item. Returns ``(content, content_type)``.

        Strategy:
          1. GET the metadata.
          2. If it includes one of the known URL fields, GET that URL.
             (Presigned S3 URLs don't need our Authorization header — and
             passing one can cause AWS to 400 on the signed request — so we
             only re-send the bearer when the URL is on the same host as
             the Drive itself.)
          3. Else fall back to GET /api/v1.0/items/{id}/download/ with bearer.

        ``max_bytes`` caps the in-memory payload — anything larger raises
        ``DriveApplicativeError`` rather than blowing up the worker.
        """
        metadata = self.get_item(access_token, item_id)

        download_url = None
        for key in _DOWNLOAD_URL_KEYS:
            candidate = metadata.get(key)
            if isinstance(candidate, str) and candidate.strip():
                download_url = candidate
                break

        if download_url:
            send_bearer = download_url.startswith(self.base_url + "/") or download_url.startswith(self.base_url + ":")
        else:
            download_url = f"{self.base_url}{_API_PREFIX}/items/{item_id}/download/"
            send_bearer = True

        headers = {"Authorization": f"Bearer {access_token}"} if send_bearer else {}
        try:
            resp = req.get(download_url, headers=headers, timeout=self.download_timeout, stream=False)
        except req.RequestException as exc:
            raise DriveTransientError(f"Drive download unreachable: {exc}") from exc
        if resp.status_code in (401, 403):
            raise DriveAuthError(
                f"Drive download {resp.status_code} for item {item_id}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise DriveTransientError(f"Drive download 5xx: {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"Drive download {resp.status_code}: {(resp.text or '')[:200]}")
        body = resp.content
        if max_bytes is not None and len(body) > max_bytes:
            raise DriveApplicativeError(
                f"Drive item {item_id} exceeds max_bytes={max_bytes} (got {len(body)})"
            )
        content_type = resp.headers.get("Content-Type") or metadata.get("mime_type") or "application/octet-stream"
        return body, content_type

    # ── Internals ───────────────────────────────────────────

    @staticmethod
    def _raise_for_status(resp, context: str) -> None:
        if resp.status_code in (401, 403):
            raise DriveAuthError(
                f"{context} → {resp.status_code} (token rejected by Drive)",
                status_code=resp.status_code,
            )
        if resp.status_code >= 500:
            raise DriveTransientError(f"{context} → {resp.status_code}")
        if resp.status_code >= 400:
            raise DriveApplicativeError(f"{context} → {resp.status_code}: {(resp.text or '')[:200]}")
