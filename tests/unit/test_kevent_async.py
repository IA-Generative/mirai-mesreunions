"""
Unit tests for the async / job-based mode of the Kevent client
(submit_job → wait_for_job polling → result inline).
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_requests_stub():
    requests_stub = types.ModuleType("requests")

    class _Resp:
        def __init__(self, status_code=200, json_data=None, text=""):
            self.status_code = status_code
            self._json = json_data if json_data is not None else {}
            self.text = text

        def json(self):
            if isinstance(self._json, Exception):
                raise self._json
            return self._json

    class _RequestException(Exception):
        pass

    requests_stub.RequestException = _RequestException
    requests_stub.post = MagicMock()
    requests_stub.get = MagicMock()
    requests_stub._Resp = _Resp
    sys.modules["requests"] = requests_stub
    return requests_stub


_REQ = _install_requests_stub()


def _resp(status_code=200, json_data=None, text=""):
    return _REQ._Resp(status_code=status_code, json_data=json_data, text=text)


MODULE_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "kevent_client.py")
SPEC = importlib.util.spec_from_file_location("kevent_async_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def _client():
    return MOD.KeventClient(
        gateway_url="https://gateway.api.example.com",
        api_key="testkey-deadbeef",
    )


@pytest.fixture(autouse=True)
def _reset():
    _REQ.post.reset_mock(side_effect=True, return_value=True)
    _REQ.get.reset_mock(side_effect=True, return_value=True)


# ─── submit_job ───────────────────────────────────────────────────────────

def test_submit_job_returns_job_id_on_202():
    _REQ.post.return_value = _resp(202, json_data={
        "job_id": "abc-123", "service_type": "audio",
        "model": "whisper", "status": "pending",
    })
    out = _client().submit_job(b"audio", "x.mp4", "audio/mp4",
                               service_type="audio", operation="transcription",
                               model="whisper-large-v3-turbo")
    assert out == "abc-123"
    args, kwargs = _REQ.post.call_args
    assert args[0].endswith("/jobs/audio")
    assert kwargs["data"]["operation"] == "transcription"
    assert kwargs["data"]["model"] == "whisper-large-v3-turbo"


def test_submit_job_passes_extra_form_fields():
    _REQ.post.return_value = _resp(202, json_data={"job_id": "x"})
    _client().submit_job(b"a", "x.mp4", "audio/mp4",
                         service_type="audio", operation="transcription",
                         extra_form={"language": "fr", "response_format": "verbose_json"})
    _, kwargs = _REQ.post.call_args
    assert kwargs["data"]["language"] == "fr"
    assert kwargs["data"]["response_format"] == "verbose_json"


def test_submit_job_drops_none_extra_fields():
    _REQ.post.return_value = _resp(202, json_data={"job_id": "x"})
    _client().submit_job(b"a", "x.mp4", "audio/mp4",
                         service_type="audio", operation="transcription",
                         extra_form={"language": None})
    _, kwargs = _REQ.post.call_args
    assert "language" not in kwargs["data"]


def test_submit_job_401_raises_auth_error():
    _REQ.post.return_value = _resp(401, text="bad apikey")
    with pytest.raises(MOD.KeventAuthError):
        _client().submit_job(b"a", "x.mp4", "audio/mp4",
                             service_type="audio", operation="transcription")


def test_submit_job_503_raises_transient_error():
    _REQ.post.return_value = _resp(503)
    with pytest.raises(MOD.KeventTransientError):
        _client().submit_job(b"a", "x.mp4", "audio/mp4",
                             service_type="audio", operation="transcription")


def test_submit_job_missing_job_id_raises_applicative_error():
    _REQ.post.return_value = _resp(202, json_data={"status": "pending"})
    with pytest.raises(MOD.KeventApplicativeError):
        _client().submit_job(b"a", "x.mp4", "audio/mp4",
                             service_type="audio", operation="transcription")


def test_submit_job_network_error_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("conn reset")
    with pytest.raises(MOD.KeventTransientError):
        _client().submit_job(b"a", "x.mp4", "audio/mp4",
                             service_type="audio", operation="transcription")


# ─── get_job ─────────────────────────────────────────────────────────────

def test_get_job_returns_status_body():
    _REQ.get.return_value = _resp(200, json_data={
        "job_id": "abc", "status": "processing", "service_type": "audio",
    })
    out = _client().get_job("audio", "abc")
    assert out["status"] == "processing"
    args, _ = _REQ.get.call_args
    assert args[0].endswith("/jobs/audio/abc")


def test_get_job_404_raises_applicative_error():
    _REQ.get.return_value = _resp(404, text="job not found")
    with pytest.raises(MOD.KeventApplicativeError):
        _client().get_job("audio", "missing")


def test_get_job_5xx_raises_transient_error():
    _REQ.get.return_value = _resp(503)
    with pytest.raises(MOD.KeventTransientError):
        _client().get_job("audio", "x")


# ─── wait_for_job ────────────────────────────────────────────────────────

def _build_get_sequence(*payloads):
    """Make `get` return successive _Resp(200, …) bodies."""
    iter_payloads = iter(payloads)
    def side(*a, **kw):
        return _resp(200, json_data=next(iter_payloads))
    _REQ.get.side_effect = side


def test_wait_for_job_returns_inline_result_on_completed():
    _build_get_sequence(
        {"status": "pending"},
        {"status": "processing"},
        {"status": "completed", "result": {"text": "Bonjour."}},
    )
    sleep_calls = []
    out = _client().wait_for_job(
        "audio", "abc",
        poll_interval=1, timeout=60,
        sleep_fn=lambda s: sleep_calls.append(s),
        time_fn=iter([0, 1, 2, 3, 4]).__next__,
    )
    assert out == {"text": "Bonjour."}
    # 2 sleeps (between the 3 GET calls — after the 3rd we hit terminal status)
    assert sleep_calls == [1, 1]


def test_wait_for_job_invokes_on_status_at_each_change():
    _build_get_sequence(
        {"status": "pending"},
        {"status": "pending"},  # no change → no callback
        {"status": "processing"},
        {"status": "completed", "result": "ok"},
    )
    statuses = []
    _client().wait_for_job(
        "audio", "abc",
        poll_interval=0, timeout=60,
        on_status=statuses.append,
        sleep_fn=lambda s: None,
        time_fn=iter(range(50)).__next__,
    )
    # Each unique status reported, including terminal
    assert statuses == ["pending", "processing", "completed"]


def test_wait_for_job_failed_raises_applicative_error():
    _build_get_sequence(
        {"status": "processing"},
        {"status": "failed", "error": "model OOM"},
    )
    with pytest.raises(MOD.KeventApplicativeError, match="model OOM"):
        _client().wait_for_job(
            "audio", "abc", poll_interval=0, timeout=60,
            sleep_fn=lambda s: None,
            time_fn=iter(range(50)).__next__,
        )


def test_wait_for_job_completed_without_result_raises_applicative():
    _build_get_sequence({"status": "completed"})  # missing 'result'
    with pytest.raises(MOD.KeventApplicativeError, match="no result"):
        _client().wait_for_job(
            "audio", "abc", poll_interval=0, timeout=60,
            sleep_fn=lambda s: None,
            time_fn=iter(range(50)).__next__,
        )


def test_wait_for_job_timeout_raises_KeventTimeoutError():
    # Always processing, never terminal
    def always_processing(*a, **kw):
        return _resp(200, json_data={"status": "processing"})
    _REQ.get.side_effect = always_processing
    # time_fn jumps past the deadline on the second iteration
    with pytest.raises(MOD.KeventTimeoutError):
        _client().wait_for_job(
            "audio", "abc", poll_interval=0, timeout=10,
            sleep_fn=lambda s: None,
            time_fn=iter([0, 0, 100]).__next__,
        )


def test_wait_for_job_timeout_is_transient_error_subclass():
    """Caller can catch KeventTransientError to handle both 5xx + timeout."""
    assert issubclass(MOD.KeventTimeoutError, MOD.KeventTransientError)


def test_wait_for_job_on_status_failure_does_not_break_polling():
    """An on_status callback that throws is logged but doesn't abort the wait."""
    _build_get_sequence(
        {"status": "processing"},
        {"status": "completed", "result": "ok"},
    )
    def boom(_):
        raise RuntimeError("ui push died")
    out = _client().wait_for_job(
        "audio", "abc", poll_interval=0, timeout=60,
        on_status=boom,
        sleep_fn=lambda s: None,
        time_fn=iter(range(50)).__next__,
    )
    assert out == "ok"


# ─── transcribe_async / diarize_async (drop-in wrappers) ─────────────────

def test_transcribe_async_chains_submit_then_wait():
    """submit returns job_id, then GET completes with the inline result."""
    _REQ.post.return_value = _resp(202, json_data={"job_id": "j-1"})
    _build_get_sequence({"status": "completed", "result": {"text": "hi"}})
    out = _client().transcribe_async(
        b"audio", "x.mp4", "audio/mp4",
        language="fr", poll_interval=0, timeout=60,
    )
    assert out == {"text": "hi"}
    # POST was called once with the right form fields
    _, post_kw = _REQ.post.call_args
    assert post_kw["data"]["operation"] == "transcription"
    assert post_kw["data"]["model"] == "faster-whisper-large-v3-turbo"
    assert post_kw["data"]["language"] == "fr"
    assert post_kw["data"]["response_format"] == "verbose_json"
    # Word-level timestamps requis pour le karaoke UI (mig 017).
    assert post_kw["data"]["word_timestamps"] == "true"


def test_diarize_async_chains_submit_then_wait():
    _REQ.post.return_value = _resp(202, json_data={"job_id": "j-2"})
    _build_get_sequence({"status": "completed", "result": {"segments": []}})
    out = _client().diarize_async(b"audio", "x.mp4", "audio/mp4",
                                  poll_interval=0, timeout=60)
    assert out == {"segments": []}
    _, post_kw = _REQ.post.call_args
    assert post_kw["data"]["operation"] == "diarization"
    assert post_kw["data"]["model"] == "pyannote-diarization"
