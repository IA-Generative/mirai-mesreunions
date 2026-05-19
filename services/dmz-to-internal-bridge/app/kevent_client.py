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

import json as _json
import logging
import time
import urllib.error
import urllib.request
import uuid
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
        diarization_backend: str = "kevent",
        diarization_vm_url: str = "",
    ):
        if not gateway_url:
            raise ValueError("KEVENT_GATEWAY_URL is required to build KeventClient")
        if not api_key:
            raise ValueError("KEVENT_API_KEY is required to build KeventClient")
        if diarization_backend not in ("kevent", "vm-direct"):
            raise ValueError(
                f"diarization_backend must be 'kevent' or 'vm-direct', got {diarization_backend!r}"
            )
        if diarization_backend == "vm-direct" and not diarization_vm_url:
            raise ValueError(
                "diarization_backend='vm-direct' requires diarization_vm_url to be set"
            )
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.transcription_model = transcription_model
        self.diarization_model = diarization_model
        self.timeout = timeout
        self.diarization_backend = diarization_backend
        self.diarization_vm_url = diarization_vm_url.rstrip("/")

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _whisper_safe_filename(filename: str) -> str:
        """Renomme l'extension pour matcher la whitelist du gateway Whisper.

        Le gateway accepte ``.mp3 .wav .m4a .ogg .flac``. Le audio-normalizer
        produit du ``.mp4`` (container MP4 + AAC), qui est sémantiquement
        identique à ``.m4a`` côté contenu. On renomme juste l'extension du
        multipart sans toucher aux bytes, sinon le gateway répond
        ``400: extension ".mp4" not accepted``.
        """
        if not filename:
            return filename
        lower = filename.lower()
        if lower.endswith(".mp4"):
            return filename[:-4] + ".m4a"
        return filename

    def _auth_header(self) -> dict:
        # Le gateway Mirai a migré (validé empiriquement 2026-05-11) :
        # l'ancien header non-standard ``apikey: Bearer <token>`` retourne
        # désormais ``401 missing token`` sur tous les endpoints. La clé
        # active depuis llm-credentials.txt s'utilise avec le header standard
        # ``Authorization: Bearer <token>``. On garde le préfixe "Bearer "
        # tel quel — c'est le format dans llm-credentials.txt et la doc gateway.
        prefix = "" if self.api_key.startswith("Bearer ") else "Bearer "
        return {"Authorization": f"{prefix}{self.api_key}"}

    @staticmethod
    def _raise_for_status(resp, context: str) -> None:
        if resp.status_code in (401, 403):
            raise KeventAuthError(f"{context} → {resp.status_code} (Authorization header rejected by Kevent)")
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
        files = {"file": (self._whisper_safe_filename(filename), audio_bytes, content_type)}
        data = {
            "model": self.transcription_model,
            "response_format": response_format,
            "word_timestamps": "true",
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
        files = {"file": (self._whisper_safe_filename(filename), audio_bytes, content_type)}
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

    def list_jobs(
        self,
        service_type: str = "audio",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """GET /jobs?service_type=...&limit=...&offset=... — listing live
        de la file d'attente (scopé sur la clé API = consumer).

        Confirmé opérationnel 2026-05-12. La réponse contient :
          { consumer, total, limit, offset, jobs: [{job_id, service_type,
            model, status, queue_position?, created_at, updated_at, error?}] }

        À noter : les jobs ``completed`` sont supprimés après pickup côté
        gateway → invisibles ici (ne pas tenter d'en déduire un throughput).
        ``queue_position`` (1-based) n'est présent que sur les ``pending``.
        """
        url = f"{self.gateway_url}/jobs"
        params = {"service_type": service_type, "limit": limit, "offset": offset}
        try:
            resp = req.get(url, headers=self._auth_header(),
                           params=params, timeout=self.timeout)
        except req.RequestException as exc:
            raise KeventTransientError(f"Kevent list_jobs unreachable: {exc}") from exc
        self._raise_for_status(resp, "GET /jobs")
        try:
            return resp.json()
        except Exception as exc:
            raise KeventApplicativeError(f"Kevent list_jobs non-JSON: {exc}") from exc

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
        on_submitted: Optional[Callable[[str], None]] = None,
        initial_prompt: Optional[str] = None,
    ) -> dict:
        """Async equivalent of ``transcribe`` — same return shape, polled.

        ``on_submitted(job_id)`` is invoked exactly once, juste après que le
        gateway ait accepté le job (avant le premier poll). Permet au caller
        de persister le job_id en DB pour reprise au boot — cf.
        Phase 2bis dans puller.py.

        ``initial_prompt`` (meeting-prep v2 §5.1bis) : si fourni, passé via
        ``extra_form`` au gateway. Whisper l'utilise comme biais lexical
        (limite 244 tokens). Le gateway Mirai peut ignorer le champ — pas
        de régression dans ce cas, le champ supplémentaire est silently
        dropped. Logger en INFO la longueur pour audit.
        """
        extra: dict = {
            "response_format": response_format,
            "word_timestamps": "true",
        }
        if language:
            extra["language"] = language
        if initial_prompt:
            extra["initial_prompt"] = initial_prompt
            logger.info(
                "kevent transcribe_async: passing initial_prompt (%d chars)",
                len(initial_prompt),
            )
        job_id = self.submit_job(
            audio_bytes, filename, content_type,
            service_type=service_type, operation=operation,
            model=self.transcription_model, extra_form=extra,
        )
        if on_submitted is not None:
            try:
                on_submitted(job_id)
            except Exception:
                logger.exception("on_submitted callback failed for job %s", job_id)
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
        on_submitted: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Async equivalent of ``diarize``.

        Voir ``transcribe_async`` pour la sémantique de ``on_submitted``.

        Si ``diarization_backend='vm-direct'``, redirige vers ``_diarize_vm``
        (sync) — la VM répond en <3 min sur 105 min d'audio, le modèle async
        polling n'apporte rien. ``on_status`` / ``on_submitted`` ne sont pas
        appelés dans ce cas (pas de job_id à persister).
        """
        if self.diarization_backend == "vm-direct":
            return self._diarize_vm(audio_bytes, filename, content_type)
        job_id = self.submit_job(
            audio_bytes, filename, content_type,
            service_type=service_type, operation=operation,
            model=self.diarization_model,
        )
        if on_submitted is not None:
            try:
                on_submitted(job_id)
            except Exception:
                logger.exception("on_submitted callback failed for job %s", job_id)
        return self.wait_for_job(
            service_type, job_id,
            poll_interval=poll_interval, timeout=timeout,
            on_status=on_status,
        )

    # Helper pour la reprise post-restart : on a déjà un job_id en DB, on
    # rejoint juste le poll sans re-submit. Renvoie le résultat final
    # (transcription/diarization) ou raise comme wait_for_job.
    def resume_job(
        self,
        service_type: str,
        job_id: str,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
        on_status: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Poll un job déjà soumis (utile au boot pour récupérer un orphan)."""
        return self.wait_for_job(
            service_type, job_id,
            poll_interval=poll_interval, timeout=timeout,
            on_status=on_status,
        )

    # ── Step 2: diarisation ─────────────────────────────────

    def diarize(self, audio_bytes: bytes, filename: str, content_type: str) -> dict:
        """
        Diarisation synchrone. Retourne ``{segments: [{speaker, start, end}, …],
        num_speakers, duration, processing_time}``.

        Si ``diarization_backend='vm-direct'``, tape directement le container
        diarization-api via ``DIARIZATION_VM_URL``. Sinon, POST
        ``/v1/audio/diarizations`` sur le gateway Kevent.
        """
        if self.diarization_backend == "vm-direct":
            return self._diarize_vm(audio_bytes, filename, content_type)

        url = f"{self.gateway_url}/v1/audio/diarizations"
        files = {"file": (self._whisper_safe_filename(filename), audio_bytes, content_type)}
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

    # ── Backend vm-direct ────────────────────────────────────
    #
    # Implémenté avec stdlib urllib.request (et PAS requests) parce que
    # ``requests==2.32.3`` envoie un multipart qui fait rejeter le POST par
    # nginx en HTTP 400 lorsqu'on passe par le gate devant la VM (vérifié
    # empiriquement le 2026-05-16 : curl OK, httpx OK, urllib OK, requests
    # KO sur le même payload + même header). On évite d'ajouter ``httpx``
    # comme dépendance juste pour ça — stdlib suffit.

    def _diarize_vm(
        self,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
    ) -> dict:
        """POST direct sur ``{DIARIZATION_VM_URL}/v1/audio/diarizations``.

        Auth réutilise ``self.api_key`` comme ``Authorization: Bearer``
        (l'nginx-gate devant la VM accepte le token kevent). Erreurs
        classées dans les mêmes familles que les appels gateway.
        """
        url = f"{self.diarization_vm_url}/v1/audio/diarizations"
        boundary = uuid.uuid4().hex
        crlf = b"\r\n"
        body = (
            b"--" + boundary.encode() + crlf
            + (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"'
            ).encode() + crlf
            + f"Content-Type: {content_type}".encode() + crlf + crlf
            + audio_bytes + crlf
            + b"--" + boundary.encode() + b"--" + crlf
        )
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        headers.update(self._auth_header())
        request = urllib.request.Request(
            url, data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            payload = (exc.read() or b"")[:200].decode(errors="replace")
            if exc.code in (401, 403):
                raise KeventAuthError(
                    f"POST {url} → {exc.code} (Authorization rejected by VM gate)"
                ) from exc
            if exc.code >= 500:
                raise KeventTransientError(
                    f"POST {url} → {exc.code}: {payload}"
                ) from exc
            raise KeventApplicativeError(
                f"POST {url} → {exc.code}: {payload}"
            ) from exc
        except urllib.error.URLError as exc:
            raise KeventTransientError(
                f"VM diarization unreachable at {url}: {exc.reason}"
            ) from exc
        try:
            return _json.loads(raw)
        except Exception as exc:
            raise KeventApplicativeError(
                f"VM diarization returned non-JSON: {exc}"
            ) from exc
