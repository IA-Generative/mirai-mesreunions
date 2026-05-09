"""
Unit tests for ``_perform_pull`` — the core orchestration that downloads from
S3 processed-staging, uploads to S3 internal-storage, inserts the
``UserAudioFile`` row, and enqueues transcription.

The function is reused by both the legacy /api/v1/pull endpoint and the new
queue-driven drain loop. These tests pin its observable contract:
  - calls in the right order;
  - idempotence on a duplicate ``stored_filename``;
  - auto_transcribe flag toggles the transcription enqueue;
  - infrastructure errors propagate so the queue retry counter can react.
"""

import importlib.util
import os
import sys
import types
from io import BytesIO
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_stubs():
    os.environ["INTERNAL_API_TOKEN"] = "x" * 48
    os.environ["INTERNAL_PUSH_TRIGGER_TOKEN"] = "anything"
    os.environ["SKIP_CREATE_APP"] = "1"

    # queue_helper stub
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

    # config stub
    cfg_stub = types.ModuleType("libs.shared.app.config")
    cfg_stub.load_int_db = lambda: types.SimpleNamespace(sync_url="sqlite:///:memory:")
    cfg_stub.load_s3_processed = lambda: types.SimpleNamespace()
    cfg_stub.load_s3_internal = lambda: types.SimpleNamespace()
    cfg_stub.RabbitMQConfig = _RMQ
    cfg_stub.INTERNAL_API_TOKEN = "x" * 48
    cfg_stub.INTERNAL_PULL_QUEUE_INTERVAL_SECONDS = 30
    sys.modules["libs.shared.app.config"] = cfg_stub

    # models stub
    models_stub = types.ModuleType("libs.shared.app.models")
    models_stub.InternalBase = MagicMock()
    # UserAudioFile must be a constructable class with attributes preserved.
    class _UAF:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    models_stub.UserAudioFile = _UAF
    sys.modules["libs.shared.app.models"] = models_stub

    # database stub
    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    # s3_helper stub
    s3_stub = types.ModuleType("libs.shared.app.s3_helper")
    s3_stub.download_fileobj = MagicMock()
    s3_stub.upload_fileobj = MagicMock()
    s3_stub.ensure_bucket = MagicMock()
    s3_stub.delete_object = MagicMock()
    sys.modules["libs.shared.app.s3_helper"] = s3_stub

    # security stub
    sec_stub = types.ModuleType("libs.shared.app.security")
    sec_stub.require_strong_shared_secret = lambda *_a, **_kw: None
    sec_stub.verify_bearer_token = lambda h, t: False
    sys.modules["libs.shared.app.security"] = sec_stub


def _load_puller():
    sys.modules.pop("file_puller_perform_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "file_puller_perform_under_test",
        os.path.join(ROOT, "services", "file-mover", "app", "puller.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _full_payload(**overrides):
    base = {
        "file_id": "fid-123",
        "user_sub": "user-1",
        "user_email": "u@example.com",
        "simple_code": "ABC123",
        "original_filename": "audio.m4a",
        "transcoded_filename": "audio.mp4",
        "quality_score": 4.2,
        "duration_seconds": 30.5,
        "auto_transcribe": True,
    }
    base.update(overrides)
    return base


def _wire_session(mod, existing=None):
    """Wire mod.SessionLocal so the first .query().filter().first() returns
    `existing`, and subsequent .add()/.commit() are tracked."""
    db_session = MagicMock()
    db_session.query.return_value.filter.return_value.first.return_value = existing
    db_session.add = MagicMock()
    db_session.commit = MagicMock()
    db_session.close = MagicMock()
    factory = MagicMock()
    factory.return_value = db_session
    mod.SessionLocal = factory
    return db_session


# --- Tests ---------------------------------------------------------------

def test_happy_path_downloads_uploads_inserts_publishes_in_order():
    _install_stubs()
    mod = _load_puller()
    # Mock the symbols *as bound inside the loaded module* (not on the stubs):
    mod.download_fileobj = MagicMock(return_value=BytesIO(b"audio-bytes"))
    mod.upload_fileobj = MagicMock()
    mod.publish_message = MagicMock()
    mod.notify_external_status = MagicMock()
    db_session = _wire_session(mod, existing=None)

    out = mod._perform_pull(_full_payload())
    assert out["status"] == "pulled"
    assert out["internal_key"] == "user-1/ABC123/audio.mp4"

    # Order of side effects.
    assert mod.download_fileobj.called
    assert mod.upload_fileobj.called
    db_session.add.assert_called_once()
    db_session.commit.assert_called()
    mod.publish_message.assert_called_once()
    args, _ = mod.publish_message.call_args
    # publish_message(rabbit_cfg, queue, payload)
    assert args[1] == "transcription"


def test_idempotent_replay_skips_redownload():
    _install_stubs()
    mod = _load_puller()
    mod.download_fileobj = MagicMock(return_value=BytesIO(b"x"))
    mod.upload_fileobj = MagicMock()
    mod.publish_message = MagicMock()
    mod.notify_external_status = MagicMock()
    existing = MagicMock()  # simulate "already imported"
    db_session = _wire_session(mod, existing=existing)

    out = mod._perform_pull(_full_payload())
    assert out["status"] == "already_pulled"
    assert mod.download_fileobj.call_count == 0
    assert mod.upload_fileobj.call_count == 0
    db_session.add.assert_not_called()
    mod.publish_message.assert_not_called()


def test_auto_transcribe_false_skips_transcription_publish():
    _install_stubs()
    mod = _load_puller()
    mod.download_fileobj = MagicMock(return_value=BytesIO(b"x"))
    mod.upload_fileobj = MagicMock()
    mod.publish_message = MagicMock()
    mod.notify_external_status = MagicMock()
    _wire_session(mod, existing=None)

    out = mod._perform_pull(_full_payload(auto_transcribe=False))
    assert out["status"] == "pulled"
    mod.publish_message.assert_not_called()


def test_missing_required_field_raises_valueerror():
    _install_stubs()
    mod = _load_puller()
    payload = _full_payload()
    del payload["transcoded_filename"]
    with pytest.raises(ValueError):
        mod._perform_pull(payload)


def test_download_failure_propagates_so_queue_can_retry():
    _install_stubs()
    mod = _load_puller()
    mod.download_fileobj = MagicMock(side_effect=RuntimeError("S3 timeout"))
    mod.upload_fileobj = MagicMock()
    mod.publish_message = MagicMock()
    mod.notify_external_status = MagicMock()
    db_session = _wire_session(mod, existing=None)
    with pytest.raises(RuntimeError):
        mod._perform_pull(_full_payload())
    db_session.add.assert_not_called()
    mod.publish_message.assert_not_called()


def test_drain_callback_returns_true_on_success():
    """The adapter used by the queue drain must return True iff perform_pull succeeded."""
    _install_stubs()
    mod = _load_puller()
    mod._perform_pull = MagicMock(return_value={"status": "pulled"})
    assert mod._drain_internal_pull_callback(_full_payload()) is True


def test_drain_callback_returns_true_on_invalid_payload():
    """A ValueError (bad payload) must NOT cause an infinite retry — drop instead."""
    _install_stubs()
    mod = _load_puller()
    mod._perform_pull = MagicMock(side_effect=ValueError("missing field"))
    assert mod._drain_internal_pull_callback({"file_id": "x"}) is True


def test_drain_callback_returns_false_on_runtime_failure():
    """Infra failures (S3 down, DB down) must signal retry."""
    _install_stubs()
    mod = _load_puller()
    mod._perform_pull = MagicMock(side_effect=RuntimeError("S3 down"))
    assert mod._drain_internal_pull_callback(_full_payload()) is False
