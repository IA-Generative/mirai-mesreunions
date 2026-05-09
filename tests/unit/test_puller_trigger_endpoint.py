"""
Unit tests for file-puller's /api/v1/pull-trigger endpoint.

Covers:
  - bearer enforcement (401 on missing/wrong token);
  - optional IP allowlist (403 when source not in allowlist);
  - drain semantics (handler always returns 200 + drained count when auth OK).

We import puller.py with SKIP_CREATE_APP=1 so module load doesn't try to open
DB/S3/RabbitMQ connections. Drain is mocked so the test never opens an AMQP
socket — we only validate the surface contract of the HTTP layer.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

# These tests need Flask + SQLAlchemy + pika to import puller.py end-to-end.
# The CI image has them; a bare local Python may not. Skip cleanly so the
# rest of the unit suite can still run.
pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_stubs(trigger_token: str, ip_allowlist: str = ""):
    os.environ["INTERNAL_API_TOKEN"] = "x" * 48
    os.environ["INTERNAL_PUSH_TRIGGER_TOKEN"] = trigger_token
    os.environ["INTERNAL_PUSH_TRIGGER_IP_ALLOWLIST"] = ip_allowlist
    os.environ["SKIP_CREATE_APP"] = "1"

    # Light stubs for the heavy libs that puller.py loads.
    sys.modules.pop("libs.shared.app.queue_helper", None)
    qh_stub = types.ModuleType("libs.shared.app.queue_helper")
    qh_stub.publish_message = MagicMock()
    qh_stub.declare_queues = MagicMock()
    qh_stub.drain_queue_once = MagicMock(return_value=0)
    qh_stub.QUEUE_TRANSCRIPTION = "transcription"
    qh_stub.QUEUE_INTERNAL_PULL = "internal_pull"
    class _RMQ:
        host = "x"; port = 5672; user = "u"; password = "p"; vhost = "/"
    qh_stub.RabbitMQConfig = _RMQ
    sys.modules["libs.shared.app.queue_helper"] = qh_stub

    cfg_stub = types.ModuleType("libs.shared.app.config")
    cfg_stub.load_int_db = lambda: types.SimpleNamespace(sync_url="sqlite:///:memory:")
    cfg_stub.load_s3_processed = lambda: types.SimpleNamespace()
    cfg_stub.load_s3_internal = lambda: types.SimpleNamespace()
    cfg_stub.RabbitMQConfig = _RMQ
    cfg_stub.INTERNAL_API_TOKEN = "x" * 48
    cfg_stub.INTERNAL_PULL_QUEUE_INTERVAL_SECONDS = 30
    sys.modules["libs.shared.app.config"] = cfg_stub

    models_stub = types.ModuleType("libs.shared.app.models")
    models_stub.InternalBase = MagicMock()
    models_stub.UserAudioFile = MagicMock()
    sys.modules["libs.shared.app.models"] = models_stub

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    s3_stub = types.ModuleType("libs.shared.app.s3_helper")
    s3_stub.download_fileobj = MagicMock()
    s3_stub.upload_fileobj = MagicMock()
    s3_stub.ensure_bucket = MagicMock()
    s3_stub.delete_object = MagicMock()
    sys.modules["libs.shared.app.s3_helper"] = s3_stub

    sec_stub = types.ModuleType("libs.shared.app.security")
    sec_stub.require_strong_shared_secret = lambda *_a, **_kw: None
    # Real bearer compare: trim "Bearer " prefix and constant-time check.
    def _verify_bearer(auth_header, expected):
        if not expected:
            return False
        if not auth_header.startswith("Bearer "):
            return False
        return auth_header[len("Bearer "):] == expected
    sec_stub.verify_bearer_token = _verify_bearer
    sys.modules["libs.shared.app.security"] = sec_stub


def _load_puller():
    sys.modules.pop("file_puller_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "file_puller_under_test",
        os.path.join(ROOT, "services", "file-mover", "app", "puller.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Tests ----------------------------------------------------------------

def test_trigger_no_auth_header_returns_401():
    _install_stubs(trigger_token="secret")
    mod = _load_puller()
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger")
    assert resp.status_code == 401


def test_trigger_wrong_bearer_returns_401():
    _install_stubs(trigger_token="secret")
    mod = _load_puller()
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_trigger_correct_bearer_drains_queue_and_returns_200():
    _install_stubs(trigger_token="secret")
    mod = _load_puller()
    # Capture how many were drained — we mocked drain_queue_once to return 0
    # by default, but the handler calls _drain_internal_pull_queue which calls
    # drain_queue_once via the imported binding. Patch the local symbol.
    mod._drain_internal_pull_queue = MagicMock(return_value=3)
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"status": "ok", "drained": 3}
    mod._drain_internal_pull_queue.assert_called_once()


def test_trigger_endpoint_disabled_when_no_token_configured():
    """If INTERNAL_PUSH_TRIGGER_TOKEN is empty, every call must be rejected,
    even with a Bearer header — this prevents accidental opening when the
    secret hasn't been provisioned."""
    _install_stubs(trigger_token="")
    mod = _load_puller()
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 401


def test_trigger_body_is_ignored():
    """The endpoint is a wake-up signal, not a payload submission."""
    _install_stubs(trigger_token="secret")
    mod = _load_puller()
    mod._drain_internal_pull_queue = MagicMock(return_value=0)
    client = mod.app.test_client()
    # Garbage payload — must still be 200.
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer secret"},
                       data=b"<not-json>")
    assert resp.status_code == 200


def test_trigger_ip_allowlist_blocks_unknown_source():
    """ACL applicative redondante avec nginx whitelist."""
    _install_stubs(trigger_token="secret", ip_allowlist="10.0.0.0/8")
    mod = _load_puller()
    mod._drain_internal_pull_queue = MagicMock(return_value=0)
    # Flask test_client uses 127.0.0.1 by default — outside 10.0.0.0/8 → 403.
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 403
    mod._drain_internal_pull_queue.assert_not_called()


def test_trigger_ip_allowlist_allows_listed_source():
    _install_stubs(trigger_token="secret", ip_allowlist="127.0.0.0/8")
    mod = _load_puller()
    mod._drain_internal_pull_queue = MagicMock(return_value=2)
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    assert resp.get_json()["drained"] == 2


def test_trigger_drain_exception_returns_500():
    _install_stubs(trigger_token="secret")
    mod = _load_puller()
    mod._drain_internal_pull_queue = MagicMock(side_effect=RuntimeError("AMQP down"))
    client = mod.app.test_client()
    resp = client.post("/api/v1/pull-trigger",
                       headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 500
