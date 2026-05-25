"""Mini-client Kevent pour le fallback ASR — autonome (D14).

Volontairement réduit au sous-ensemble nécessaire à `video-ingest` :
soumission d'un job de transcription async + polling. Ne réplique PAS
le client riche de `dmz-to-internal-bridge/app/kevent_client.py` (602
lignes : diarisation, modes alternatifs, callbacks de reprise…).

À garder synchronisé avec le contrat REST de la gateway Kevent (cf.
mémoire `project_kevent_gateway_api.md`).

`requests` honore `HTTP_PROXY`/`HTTPS_PROXY` nativement (trust_env=True
par défaut), donc mode A et mode B (D15) marchent sans changement.
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger(__name__)


class KeventError(Exception):
    """Erreur Kevent (réseau, applicatif, timeout — toutes confondues en V1)."""


def submit_transcription(
    audio_bytes: bytes,
    filename: str,
    *,
    language: str | None = None,
    initial_prompt: str | None = None,
) -> str:
    """POST /jobs/audio avec operation=transcription. Renvoie le job_id."""
    gateway = os.environ.get("VIDEO_INGEST_KEVENT_GATEWAY_URL")
    api_key = os.environ.get("VIDEO_INGEST_KEVENT_API_KEY")
    if not gateway or not api_key:
        raise KeventError(
            "VIDEO_INGEST_KEVENT_GATEWAY_URL et _API_KEY requis pour fetch_audio"
        )

    url = f"{gateway.rstrip('/')}/jobs/audio"
    files = {"file": (filename, audio_bytes, "audio/m4a")}
    data: dict = {"operation": "transcription", "response_format": "verbose_json",
                  "word_timestamps": "true"}
    if language:
        data["language"] = language
    if initial_prompt:
        data["initial_prompt"] = initial_prompt

    try:
        resp = requests.post(
            url, files=files, data=data,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=int(os.environ.get("VIDEO_INGEST_KEVENT_SUBMIT_TIMEOUT", "60")),
        )
    except requests.RequestException as e:
        raise KeventError(f"Kevent submit unreachable: {e}") from e
    if resp.status_code >= 400:
        raise KeventError(f"Kevent submit HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        body = resp.json()
    except ValueError as e:
        raise KeventError(f"Kevent submit non-JSON: {e}") from e
    job_id = body.get("job_id")
    if not job_id:
        raise KeventError(f"Kevent submit sans job_id : {body}")
    log.info("kevent submit ok: job_id=%s", job_id)
    return job_id


def wait_for_result(job_id: str, *, poll_interval: float = 3.0, timeout: float = 600.0) -> dict:
    """GET /jobs/audio/{id} en boucle jusqu'à `completed` ou `failed`."""
    gateway = os.environ.get("VIDEO_INGEST_KEVENT_GATEWAY_URL")
    api_key = os.environ.get("VIDEO_INGEST_KEVENT_API_KEY")
    url = f"{gateway.rstrip('/')}/jobs/audio/{job_id}"
    deadline = time.monotonic() + timeout
    while True:
        try:
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {api_key}"},
                timeout=int(os.environ.get("VIDEO_INGEST_KEVENT_POLL_TIMEOUT", "30")),
            )
        except requests.RequestException as e:
            raise KeventError(f"Kevent poll unreachable: {e}") from e
        if resp.status_code == 404:
            raise KeventError(f"Kevent job {job_id} introuvable (déjà pickup ?)")
        if resp.status_code >= 400:
            raise KeventError(f"Kevent poll HTTP {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        status = body.get("status")
        if status == "completed":
            return body.get("result") or body
        if status == "failed":
            raise KeventError(f"Kevent job {job_id} failed: {body.get('error')}")
        if time.monotonic() > deadline:
            raise KeventError(f"Kevent job {job_id} timeout après {timeout}s")
        time.sleep(poll_interval)
