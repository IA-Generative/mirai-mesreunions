"""
HTTP client for the Mirai Kevent inference gateway.

Two operating modes :

  **Sync** (default, ``transcribe`` / ``diarize``) :
    - ``POST /v1/audio/transcriptions`` blocks until inference completes
    - ``POST /v1/audio/diarizations`` idem
    Best for short files where the connection can stay open. May be
    killed by a load-balancer or proxy on long files.

  **Async / job-based** (``submit_job`` + ``wait_for_job`` ; or the
  ``transcribe_async`` / ``diarize_async`` drop-in wrappers) :
    - ``POST /jobs/{service_type}`` returns immediately with ``{job_id,
      status: "pending"}``
    - ``GET  /jobs/{service_type}/{id}`` returns ``{status, result?,
      error?}``  (``result`` inlined when ``status=completed``)
    - the gateway DELETES the job after a successful pickup → poll until
      terminal status, then call GET exactly once.

Auth is the non-standard ``apikey: Bearer <token>`` header (the standard
``Authorization`` header is **not** used by the gateway). All paths share
the same auth.

Errors are classified into three families so the caller can react:

  - ``KeventAuthError``         : 401/403 from Kevent. The API key is
                                  rotated or restricted to a different
                                  consumer_group. No retry — needs ops.
  - ``KeventTransientError``    : 5xx, timeout, connection reset. Caller
                                  should let the queue retry counter
                                  handle it.
  - ``KeventApplicativeError``  : 4xx other (bad payload, file too large,
                                  422 model failure). No retry. Also
                                  raised when an async job ends in
                                  ``status=failed``.
  - ``KeventTimeoutError``      : async-only — polling exceeded the
                                  caller's timeout. Subclass of
                                  ``KeventTransientError`` so the queue
                                  retry counter still applies.

For transcription we request ``response_format=verbose_json`` to obtain
segment-level timestamps. The merger module uses these to align text with
diarization speaker spans.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import requests as req

logger = logging.getLogger(__name__)


# Service type registered in the kevent gateway's `config.yaml` for both
# Whisper and pyannote — they share the `audio` type and are dispatched
# inside via the `operation` form field. Configurable in case the prod
# config evolves.
DEFAULT_ASYNC_SERVICE_TYPE = "audio"
DEFAULT_TRANSCRIPTION_OPERATION = "transcription"
DEFAULT_DIARIZATION_OPERATION = "diarization"

# Terminal job statuses returned by GET /jobs/{type}/{id}.
_TERMINAL_STATUSES = {"completed", "failed"}


# ─── Exceptions ────────────────────────────────────────────────

class KeventError(Exception):
    """Base class — never raised directly."""


class KeventAuthError(KeventError):
    """API key rotated / wrong consumer_group. No retry."""


class KeventTransientError(KeventError):
    """5xx or network failure. Caller should let the queue retry."""


class KeventApplicativeError(KeventError):
    """4xx other than auth — bad payload, file too large, 422 model failure.

    Also raised when an async job ends in ``status=failed`` (the failure is
    deterministic for this input — retrying won't help).
    """


class KeventTimeoutError(KeventTransientError):
    """Async job didn't reach a terminal status within the polling deadline.

    Subclass of ``KeventTransientError`` so the queue retry counter still
    applies; a longer queue retry timer + bigger ``KEVENT_ASYNC_TIMEOUT_SECONDS``
    will eventually let the next attempt complete.
    """


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

    # ── Async job submission + polling ────────────────────────
    #
    # Endpoints (cf kevent-ai cmd/gateway/main.go) :
    #   POST /jobs/{service_type}            — submit, returns 202 + {job_id, status: pending}
    #   GET  /jobs/{service_type}/{id}       — status + inlined result when completed
    #
    # Important: the gateway DELETES the job from Redis + S3 after a
    # successful pickup of a ``completed`` result. So we poll until status
    # is terminal (completed | failed) and only then make the final GET
    # which returns the body. A second GET would return 404.

    def submit_job(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        service_type: str,
        operation: str,
        model: Optional[str] = None,
        extra_form: Optional[dict] = None,
    ) -> str:
        """POST /jobs/{service_type}. Returns the job_id string.

        ``service_type`` is the registered type in the gateway config (e.g.
        ``"audio"``). ``operation`` selects between transcription/translation/
        diarization within that type. ``model`` overrides the type's default.
        ``extra_form`` lets callers pass per-call form fields (e.g.
        ``{"language": "fr"}``) without bloating the signature.
        """
        url = f"{self.gateway_url}/jobs/{service_type}"
        files = {"file": (filename, audio_bytes, content_type)}
        data = {"operation": operation}
        if model:
            data["model"] = model
        if extra_form:
            data.update({k: v for k, v in extra_form.items() if v is not None})
        try:
            resp = req.post(
                url, files=files, data=data,
                headers=self._auth_header(), timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise KeventTransientError(f"Kevent submit_job unreachable: {exc}") from exc
        self._raise_for_status(resp, f"POST /jobs/{service_type}")
        try:
            payload = resp.json()
        except Exception as exc:
            raise KeventApplicativeError(f"Kevent submit_job non-JSON: {exc}") from exc
        job_id = payload.get("job_id")
        if not job_id:
            raise KeventApplicativeError(f"Kevent submit_job missing job_id: {payload}")
        logger.info("Kevent submitted: job_id=%s service_type=%s operation=%s",
                    job_id, service_type, operation)
        return job_id

    def get_job(self, service_type: str, job_id: str) -> dict:
        """GET /jobs/{service_type}/{id}. Returns the parsed JSON.

        Caller MUST check ``status`` and stop polling once it's terminal —
        a follow-up GET on a completed job will return 404 (the gateway
        deletes after pickup).
        """
        url = f"{self.gateway_url}/jobs/{service_type}/{job_id}"
        try:
            resp = req.get(url, headers=self._auth_header(), timeout=self.timeout)
        except req.RequestException as exc:
            raise KeventTransientError(f"Kevent get_job unreachable: {exc}") from exc
        if resp.status_code == 404:
            raise KeventApplicativeError(
                f"Kevent job {job_id} not found (already picked up?)"
            )
        self._raise_for_status(resp, f"GET /jobs/{service_type}/{job_id}")
        try:
            return resp.json()
        except Exception as exc:
            raise KeventApplicativeError(f"Kevent get_job non-JSON: {exc}") from exc

    def wait_for_job(
        self,
        service_type: str,
        job_id: str,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        on_status: Optional[Callable[[str], None]] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> dict:
        """Poll get_job until status is terminal, then return the full body.

        ``on_status(status)`` is called every time the status changes — used
        by callers to push intermediate progress (queued, processing) to the
        DB / UI without re-implementing state tracking. ``sleep_fn`` and
        ``time_fn`` are injected for testability.

        Raises:
          - KeventApplicativeError if the job ends in ``status=failed``
          - KeventTimeoutError if the deadline elapses before any terminal status
        """
        deadline = time_fn() + timeout
        last_status: Optional[str] = None
        while True:
            body = self.get_job(service_type, job_id)
            status = (body.get("status") or "").lower()
            if status != last_status:
                logger.info("Kevent job %s status %s → %s", job_id, last_status, status)
                if on_status is not None:
                    try:
                        on_status(status)
                    except Exception:
                        logger.exception("on_status callback failed (non-fatal)")
                last_status = status
            if status in _TERMINAL_STATUSES:
                if status == "failed":
                    err = body.get("error") or "kevent reported job failed without an error message"
                    raise KeventApplicativeError(f"Kevent job {job_id} failed: {err}")
                # status == completed
                if "result" not in body:
                    raise KeventApplicativeError(
                        f"Kevent job {job_id} completed but no result in body"
                    )
                return body["result"]
            if time_fn() >= deadline:
                raise KeventTimeoutError(
                    f"Kevent job {job_id} still {status!r} after {timeout}s"
                )
            sleep_fn(poll_interval)

    # ── Async drop-in wrappers (same return shape as the sync ones) ──

    def transcribe_async(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        language: Optional[str] = None,
        response_format: str = "verbose_json",
        service_type: str = DEFAULT_ASYNC_SERVICE_TYPE,
        operation: str = DEFAULT_TRANSCRIPTION_OPERATION,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Async equivalent of ``transcribe`` — same return shape, polled."""
        extra: dict = {"response_format": response_format}
        if language:
            extra["language"] = language
        job_id = self.submit_job(
            audio_bytes, filename, content_type,
            service_type=service_type, operation=operation,
            model=self.transcription_model, extra_form=extra,
        )
        return self.wait_for_job(
            service_type, job_id,
            poll_interval=poll_interval, timeout=timeout,
            on_status=on_status,
        )

    def diarize_async(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        service_type: str = DEFAULT_ASYNC_SERVICE_TYPE,
        operation: str = DEFAULT_DIARIZATION_OPERATION,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Async equivalent of ``diarize``."""
        job_id = self.submit_job(
            audio_bytes, filename, content_type,
            service_type=service_type, operation=operation,
            model=self.diarization_model,
        )
        return self.wait_for_job(
            service_type, job_id,
            poll_interval=poll_interval, timeout=timeout,
            on_status=on_status,
        )

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
