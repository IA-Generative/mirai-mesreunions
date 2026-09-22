"""Bout en bout : la passerelle Kevent répond « token inactive ».

Constaté en production le 2026-09-16 (``GET /jobs → 401 {"error":"token
inactive"}``) : la clé ``KEVENT_API_KEY`` n'est plus acceptée. La même clé
sert au dépôt des jobs de transcription — donc AUCUN audio ne peut plus
être transcrit tant que le secret n'est pas renouvelé.

Ces tests figent ce que le système doit faire dans cet état : refuser
proprement (``KeventAuthError``, jamais une boucle ni un « en cours »
éternel) et répondre 503 « file indisponible » à l'interface.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

pytest.importorskip("requests")
pytest.importorskip("flask")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.e2e._fakes import FakeService  # noqa: E402

KEVENT_CLIENT = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "kevent_client.py")


def _gateway_token_inactive(req):
    return 401, '{"error":"token inactive"}'


@pytest.fixture
def gateway():
    svc = FakeService(_gateway_token_inactive).start()
    yield svc
    svc.stop()


@pytest.fixture(scope="module")
def kevent_client_mod():
    stub = sys.modules.get("requests")
    if stub is not None and not getattr(stub, "__file__", None):
        sys.modules.pop("requests")
    spec = importlib.util.spec_from_file_location("kevent_client_e2e", KEVENT_CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(mod, gateway):
    return mod.KeventClient(gateway_url=gateway.url, api_key="Bearer dead-token", timeout=5)


def test_list_jobs_raises_auth_error(kevent_client_mod, gateway):
    with pytest.raises(kevent_client_mod.KeventAuthError) as exc:
        _client(kevent_client_mod, gateway).list_jobs(service_type="audio", limit=50)
    assert "401" in str(exc.value)
    (call,) = gateway.calls("GET")
    assert call.path.endswith("/jobs")
    assert call.headers.get("Authorization") == "Bearer dead-token"


def test_submit_job_raises_auth_error_and_does_not_retry(kevent_client_mod, gateway):
    """Un dépôt refusé pour authentification ne doit pas être rejoué :
    la clé ne redeviendra pas valide en réessayant."""
    with pytest.raises(kevent_client_mod.KeventAuthError):
        _client(kevent_client_mod, gateway).submit_job(
            audio_bytes=b"RIFF....WAVE", filename="reunion.wav",
            content_type="audio/wav", service_type="audio", operation="transcription",
        )
    assert len(gateway.calls("POST")) == 1


def test_queue_status_endpoint_answers_503_stale(gateway, monkeypatch):
    """L'interface interroge /api/v1/queue-status : réponse neutre 503 +
    ``stale`` (pas de 500, pas de position inventée)."""
    from tests.unit import test_puller_trigger_endpoint as loader

    loader._install_stubs("t" * 48)
    puller = loader._load_puller()
    import requests as real_requests
    kc_spec = importlib.util.spec_from_file_location("kevent_client_e2e_qs", KEVENT_CLIENT)
    kc = importlib.util.module_from_spec(kc_spec)
    kc_spec.loader.exec_module(kc)
    assert kc.req is real_requests
    client = kc.KeventClient(gateway_url=gateway.url, api_key="dead", timeout=5)
    monkeypatch.setattr(puller, "_build_kevent_client", lambda: client)
    puller._qs_cache.clear()

    c = puller.app.test_client()
    r = c.get("/api/v1/queue-status?service_type=audio",
              headers={"Authorization": "Bearer " + "x" * 48})
    assert r.status_code == 503
    body = r.get_json()
    assert body["stale"] is True
    assert body["pending_total"] is None and body["your_position"] is None
    assert len(gateway.calls("GET", "/jobs")) == 1

    # Le cache 5 s protège la passerelle : un second appel ne la rappelle pas.
    r2 = c.get("/api/v1/queue-status?service_type=audio",
               headers={"Authorization": "Bearer " + "x" * 48})
    assert r2.status_code == 503
    assert len(gateway.calls("GET", "/jobs")) == 1
