"""
HTTP client for the Mirai Kevent inference gateway.

Validated against ``https://gateway.api.ai.fake-domain.name`` from
the build-vm VM (cf docs/integrate-with-kevent.md). Two endpoints
are exposed for our use case:

  POST /v1/audio/transcriptions   model=faster-whisper-large-v3-turbo
  POST /v1/audio/diarizations     model=pyannote-diarization

Both are sync POST multipart, response_format=json by default. The auth
header is **non-standard** : ``apikey: Bearer <token>`` (the Authorization
header is **not** used by Kevent's gateway).

Errors are classified into three families so the caller can react:

  - ``KeventAuthError``         : 401/403 from Kevent. The API key is
                                  rotated or restricted to a different
                                  consumer_group. No retry — needs ops.
  - ``KeventTransientError``    : 5xx, timeout, connection reset. Caller
                                  should let the queue retry counter
                                  handle it.
  - ``KeventApplicativeError``  : 4xx other (bad payload, file too large,
                                  422 model failure). No retry.

For transcription we request ``response_format=verbose_json`` to obtain
segment-level timestamps. The merger module uses these to align text with
diarization speaker spans.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests as req

logger = logging.getLogger(__name__)


# ─── Exceptions ────────────────────────────────────────────────

class KeventError(Exception):
    """Base class — never raised directly."""


class KeventAuthError(KeventError):
    """API key rotated / wrong consumer_group. No retry."""


class KeventTransientError(KeventError):
    """5xx or network failure. Caller should let the queue retry."""


class KeventApplicativeError(KeventError):
    """4xx other than auth — bad payload, file too large, 422 model failure."""


# ─── Client ────────────────────────────────────────────────────

class KeventClient:
    """
    Stateless wrapper around Kevent's audio endpoints.

    The gateway uses a custom auth header — ``apikey: Bearer <token>`` —
    not the standard Authorization. This class normalises the call sites
    so callers don't have to know.
    """

    def __init__(
        self,
        gateway_url: str,
        api_key: str,
        transcription_model: str = "faster-whisper-large-v3-turbo",
        diarization_model: str = "pyannote-diarization",
        timeout: int = 600,
    ):
        if not gateway_url:
            raise ValueError("KEVENT_GATEWAY_URL is required to build KeventClient")
        if not api_key:
            raise ValueError("KEVENT_API_KEY is required to build KeventClient")
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.transcription_model = transcription_model
        self.diarization_model = diarization_model
        self.timeout = timeout

    # ── Helpers ─────────────────────────────────────────────

    def _auth_header(self) -> dict:
        # Kevent uses a non-standard "apikey" header that ALSO contains
        # the "Bearer " prefix in its value. /root/llm-credentials.txt on the VM
        # is explicit: "(laisser le `Bearer`)".
        prefix = "" if self.api_key.startswith("Bearer ") else "Bearer "
        return {"apikey": f"{prefix}{self.api_key}"}

    @staticmethod
    def _raise_for_status(resp, context: str) -> None:
        if resp.status_code in (401, 403):
            raise KeventAuthError(f"{context} → {resp.status_code} (apikey rejected by Kevent)")
        if resp.status_code >= 500:
            raise KeventTransientError(f"{context} → {resp.status_code}: {(resp.text or '')[:200]}")
        if resp.status_code == 422:
            # Kevent's UnprocessableEntity = inference failed. Treat as
            # applicative — the file is unprocessable as-is.
            raise KeventApplicativeError(f"{context} → 422 inference failed: {(resp.text or '')[:200]}")
        if resp.status_code >= 400:
            raise KeventApplicativeError(f"{context} → {resp.status_code}: {(resp.text or '')[:200]}")

    # ── Step 1: transcription ───────────────────────────────

    def transcribe(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        language: Optional[str] = None,
        response_format: str = "verbose_json",
    ) -> dict:
        """
        POST /v1/audio/transcriptions. Returns the full JSON response.

        ``verbose_json`` returns ``{task, language, duration, text,
        segments: [{id, seek, start, end, text, tokens, …}]}``. The plain
        ``json`` format omits segments — keep ``verbose_json`` so the
        merger can align with diarisation timestamps.
        """
        url = f"{self.gateway_url}/v1/audio/transcriptions"
        files = {"file": (filename, audio_bytes, content_type)}
        data = {
            "model": self.transcription_model,
            "response_format": response_format,
        }
        if language:
            data["language"] = language
        try:
            resp = req.post(
                url,
                files=files,
                data=data,
                headers=self._auth_header(),
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise KeventTransientError(f"Kevent /transcriptions unreachable: {exc}") from exc
        self._raise_for_status(resp, "POST /v1/audio/transcriptions")
        try:
            return resp.json()
        except Exception as exc:
            raise KeventApplicativeError(f"Kevent /transcriptions returned non-JSON: {exc}") from exc

    # ── Step 2: diarisation ─────────────────────────────────

    def diarize(self, audio_bytes: bytes, filename: str, content_type: str) -> dict:
        """
        POST /v1/audio/diarizations. Returns ``{segments: [{speaker, start,
        end}, …], num_speakers, duration, processing_time}``.
        """
        url = f"{self.gateway_url}/v1/audio/diarizations"
        files = {"file": (filename, audio_bytes, content_type)}
        data = {"model": self.diarization_model}
        try:
            resp = req.post(
                url,
                files=files,
                data=data,
                headers=self._auth_header(),
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise KeventTransientError(f"Kevent /diarizations unreachable: {exc}") from exc
        self._raise_for_status(resp, "POST /v1/audio/diarizations")
        try:
            return resp.json()
        except Exception as exc:
            raise KeventApplicativeError(f"Kevent /diarizations returned non-JSON: {exc}") from exc
