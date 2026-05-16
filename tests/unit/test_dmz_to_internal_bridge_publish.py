"""
Unit tests for dmz-to-internal-bridge's publish path: AMQP first, optional HTTP trigger.

Validates two contracts:
  - the AMQP publish is the success criterion (HTTP outcome is irrelevant);
  - the HTTP trigger is gated by ``INTERNAL_PUSH_TRIGGER_URL`` validity.

Stubs requests + queue_helper.publish_message before importing main.py.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_stubs(trigger_url: str = ""):
    """Reset the import environment with the desired trigger URL."""
    os.environ["INTERNAL_PUSH_TRIGGER_URL"] = trigger_url
    os.environ.setdefault("INTERNAL_API_TOKEN", "x" * 48)
    os.environ.setdefault("INTERNAL_PUSH_TRIGGER_TOKEN", "trigger-token-placeholder")

    # Stub requests.post — we just want to observe call args.
    requests_stub = types.ModuleType("requests")
    requests_stub.post = MagicMock(return_value=types.SimpleNamespace(status_code=200))

    class _ReqExc(Exception):
        pass
    requests_stub.RequestException = _ReqExc
    requests_stub.HTTPError = _ReqExc
    sys.modules["requests"] = requests_stub

    # Stub libs.shared.app.* dependencies that dmz-to-internal-bridge/main.py imports.
    sys.modules.pop("libs.shared.app.queue_helper", None)
    qh_stub = types.ModuleType("libs.shared.app.queue_helper")
    qh_stub.consume_queue = MagicMock()
    qh_stub.declare_queues = MagicMock()
    qh_stub.publish_message = MagicMock()
    qh_stub.QUEUE_FILE_READY = "file_ready"
    qh_stub.QUEUE_INTERNAL_PULL = "internal_pull"
    class _RMQ:
        host = "x"; port = 5672; user = "u"; password = "p"; vhost = "/"
    qh_stub.RabbitMQConfig = _RMQ
    sys.modules["libs.shared.app.queue_helper"] = qh_stub

    # config stub providing INTERNAL_PUSH_TRIGGER_URL & INTERNAL_API_TOKEN.
    cfg_stub = types.ModuleType("libs.shared.app.config")
    cfg_stub.load_ext_db = lambda: types.SimpleNamespace(sync_url="sqlite:///:memory:")
    cfg_stub.RabbitMQConfig = _RMQ
    cfg_stub.INTERNAL_API_TOKEN = "x" * 48
    cfg_stub.INTERNAL_PUSH_TRIGGER_URL = trigger_url
    sys.modules["libs.shared.app.config"] = cfg_stub

    # Stub models / database / security so the module-level wiring doesn't fail.
    models_stub = types.ModuleType("libs.shared.app.models")
    for n in ("ExternalBase", "UploadedFile", "UploadSession", "UploadStatus",
             "UploadTokenOption"):
        setattr(models_stub, n, MagicMock())
    sys.modules["libs.shared.app.models"] = models_stub

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    sec_stub = types.ModuleType("libs.shared.app.security")
    sec_stub.require_strong_shared_secret = lambda *_a, **_kw: None
    sys.modules["libs.shared.app.security"] = sec_stub

    # trigger_url is a real, pure module — load it normally so resolved_trigger_url
    # returns the real implementation.
    if "libs.shared.app.trigger_url" in sys.modules:
        del sys.modules["libs.shared.app.trigger_url"]
    spec_t = importlib.util.spec_from_file_location(
        "libs.shared.app.trigger_url",
        os.path.join(ROOT, "libs", "shared", "app", "trigger_url.py"),
    )
    t_mod = importlib.util.module_from_spec(spec_t)
    spec_t.loader.exec_module(t_mod)
    sys.modules["libs.shared.app.trigger_url"] = t_mod


def _load_main_module():
    sys.modules.pop("dmz_to_internal_bridge_main_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "dmz_to_internal_bridge_main_under_test",
        os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Tests ---------------------------------------------------------------

def test_trigger_disabled_when_url_empty():
    _install_stubs(trigger_url="")
    mod = _load_main_module()
    payload = {"file_id": "abc"}
    sys.modules["requests"].post.reset_mock()
    sys.modules["libs.shared.app.queue_helper"].publish_message.reset_mock()

    ok = mod.publish_internal_pull(_full_message())
    assert ok is True
    sys.modules["libs.shared.app.queue_helper"].publish_message.assert_called_once()
    sys.modules["requests"].post.assert_not_called()


def test_trigger_disabled_when_url_is_sentinel():
    _install_stubs(trigger_url="deactivate")
    mod = _load_main_module()
    sys.modules["requests"].post.reset_mock()
    ok = mod.publish_internal_pull(_full_message())
    assert ok is True
    sys.modules["requests"].post.assert_not_called()


def test_trigger_called_when_url_valid():
    _install_stubs(trigger_url="https://pull-trigger.example.com/api/v1/pull-trigger")
    mod = _load_main_module()
    sys.modules["requests"].post.reset_mock()
    sys.modules["libs.shared.app.queue_helper"].publish_message.reset_mock()
    ok = mod.publish_internal_pull(_full_message())
    assert ok is True
    # Both AMQP publish and HTTP POST happened.
    sys.modules["libs.shared.app.queue_helper"].publish_message.assert_called_once()
    assert sys.modules["requests"].post.called
    args, kwargs = sys.modules["requests"].post.call_args
    assert args[0] == "https://pull-trigger.example.com/api/v1/pull-trigger"
    # Bearer token in headers.
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization", "").startswith("Bearer ")


def test_trigger_http_failure_does_not_fail_publish():
    """The HTTP wake-up is best-effort: a 5xx must not flip publish_internal_pull to False."""
    _install_stubs(trigger_url="https://pull-trigger.example.com/api/v1/pull-trigger")
    mod = _load_main_module()
    sys.modules["requests"].post = MagicMock(
        return_value=types.SimpleNamespace(status_code=503)
    )
    sys.modules["libs.shared.app.queue_helper"].publish_message.reset_mock()
    ok = mod.publish_internal_pull(_full_message())
    assert ok is True
    sys.modules["libs.shared.app.queue_helper"].publish_message.assert_called_once()


def test_trigger_http_exception_swallowed():
    _install_stubs(trigger_url="https://pull-trigger.example.com/api/v1/pull-trigger")
    mod = _load_main_module()

    def boom(*_a, **_kw):
        raise sys.modules["requests"].RequestException("connection refused")

    sys.modules["requests"].post = MagicMock(side_effect=boom)
    sys.modules["libs.shared.app.queue_helper"].publish_message.reset_mock()
    ok = mod.publish_internal_pull(_full_message())
    assert ok is True


def test_publish_amqp_failure_returns_false():
    """If AMQP publish raises, the dmz-to-internal-bridge must report failure so file_ready
    is retried via the existing retry counter on the source queue."""
    _install_stubs(trigger_url="")
    mod = _load_main_module()
    # main.py imports publish_message symbolically, so we have to patch the
    # binding on the loaded module — not on the qh stub.
    mod.publish_message = MagicMock(side_effect=RuntimeError("RabbitMQ down"))
    ok = mod.publish_internal_pull(_full_message())
    assert ok is False


def _full_message():
    return {
        "file_id": "fid-1",
        "session_id": "sid-1",
        "user_sub": "user-1",
        "user_email": "u@example.com",
        "simple_code": "ABC123",
        "original_filename": "audio.m4a",
        "transcoded_filename": "audio.mp4",
        "quality_score": 4.5,
        "duration_seconds": 30.0,
        "auto_transcribe": True,
    }
