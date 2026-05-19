"""
Unit tests for the Kevent gateway client. Mock requests + assert that
each HTTP failure mode maps to the right exception class.
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
    requests_stub._Resp = _Resp
    sys.modules["requests"] = requests_stub
    return requests_stub


_REQ = _install_requests_stub()


def _resp(status_code=200, json_data=None, text=""):
    return _REQ._Resp(status_code=status_code, json_data=json_data, text=text)


MODULE_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "kevent_client.py")
SPEC = importlib.util.spec_from_file_location("kevent_client_under_test", MODULE_PATH)
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


# --- constructor ----------------------------------------------------------

def test_constructor_rejects_empty_gateway():
    with pytest.raises(ValueError):
        MOD.KeventClient(gateway_url="", api_key="x")


def test_constructor_rejects_empty_key():
    with pytest.raises(ValueError):
        MOD.KeventClient(gateway_url="https://x", api_key="")


# --- auth header ----------------------------------------------------------

def test_auth_header_adds_bearer_prefix_when_missing():
    c = MOD.KeventClient(gateway_url="https://x", api_key="raw-token")
    assert c._auth_header() == {"Authorization": "Bearer raw-token"}


def test_auth_header_preserves_bearer_prefix_when_present():
    c = MOD.KeventClient(gateway_url="https://x", api_key="Bearer raw-token")
    assert c._auth_header() == {"Authorization": "Bearer raw-token"}


# --- transcribe -----------------------------------------------------------

def test_transcribe_success_returns_full_json():
    _REQ.post.return_value = _resp(200, json_data={
        "task": "transcribe",
        "language": "fr",
        "duration": 12.5,
        "text": "Bonjour.",
        "segments": [{"start": 0, "end": 12.5, "text": "Bonjour."}],
    })
    out = _client().transcribe(b"audio", "x.mp4", "audio/mp4")
    assert out["text"] == "Bonjour."
    assert out["language"] == "fr"
    args, kwargs = _REQ.post.call_args
    assert "/v1/audio/transcriptions" in args[0]
    # Multipart form has the file field
    assert "file" in kwargs["files"]
    # Default response_format is verbose_json
    assert kwargs["data"]["response_format"] == "verbose_json"


def test_transcribe_passes_language_when_provided():
    _REQ.post.return_value = _resp(200, json_data={"text": "x"})
    _client().transcribe(b"audio", "x.mp4", "audio/mp4", language="en")
    _, kwargs = _REQ.post.call_args
    assert kwargs["data"]["language"] == "en"


def test_transcribe_sends_word_timestamps_true():
    """Karaoke UI dépend de words[] dans la réponse Whisper. Le gateway
    Kevent ne renvoie les ``words`` que si ``word_timestamps=true`` est
    présent dans le multipart form (cf. whisper-api-openapi.json)."""
    _REQ.post.return_value = _resp(200, json_data={"text": "x"})
    _client().transcribe(b"audio", "x.mp4", "audio/mp4")
    _, kwargs = _REQ.post.call_args
    assert kwargs["data"].get("word_timestamps") == "true"


def test_transcribe_401_raises_auth_error():
    _REQ.post.return_value = _resp(401, text="please check the consumer_group_id")
    with pytest.raises(MOD.KeventAuthError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_403_raises_auth_error():
    _REQ.post.return_value = _resp(403, text="forbidden")
    with pytest.raises(MOD.KeventAuthError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_422_raises_applicative_error():
    _REQ.post.return_value = _resp(422, text="cannot decode audio")
    with pytest.raises(MOD.KeventApplicativeError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_400_raises_applicative_error():
    _REQ.post.return_value = _resp(400, text="missing file")
    with pytest.raises(MOD.KeventApplicativeError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_500_raises_transient_error():
    _REQ.post.return_value = _resp(500, text="upstream down")
    with pytest.raises(MOD.KeventTransientError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_504_raises_transient_error():
    _REQ.post.return_value = _resp(504)
    with pytest.raises(MOD.KeventTransientError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_network_failure_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("connection refused")
    with pytest.raises(MOD.KeventTransientError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


def test_transcribe_invalid_json_response_raises_applicative_error():
    bad = _resp(200)
    bad._json = ValueError("not json")
    _REQ.post.return_value = bad
    with pytest.raises(MOD.KeventApplicativeError):
        _client().transcribe(b"audio", "x.mp4", "audio/mp4")


# --- diarize --------------------------------------------------------------

def test_diarize_success_returns_full_json():
    _REQ.post.return_value = _resp(200, json_data={
        "segments": [{"speaker": "SPEAKER_00", "start": 0, "end": 5}],
        "num_speakers": 1,
        "duration": 5.0,
    })
    out = _client().diarize(b"audio", "x.mp4", "audio/mp4")
    assert out["num_speakers"] == 1
    args, kwargs = _REQ.post.call_args
    assert "/v1/audio/diarizations" in args[0]
    assert kwargs["data"]["model"] == "pyannote-diarization"


def test_diarize_uses_authorization_bearer_header():
    # Le gateway Mirai a migré 2026-05-11 : Authorization: Bearer <token>
    # remplace l'ancien header non-standard apikey: Bearer <token>.
    _REQ.post.return_value = _resp(200, json_data={"segments": []})
    _client().diarize(b"audio", "x.mp4", "audio/mp4")
    _, kwargs = _REQ.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer testkey-deadbeef"
    assert "apikey" not in kwargs["headers"]


def test_diarize_401_raises_auth_error():
    _REQ.post.return_value = _resp(401)
    with pytest.raises(MOD.KeventAuthError):
        _client().diarize(b"audio", "x.mp4", "audio/mp4")


def test_diarize_500_raises_transient_error():
    _REQ.post.return_value = _resp(503)
    with pytest.raises(MOD.KeventTransientError):
        _client().diarize(b"audio", "x.mp4", "audio/mp4")


def test_diarize_network_failure_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("ECONNRESET")
    with pytest.raises(MOD.KeventTransientError):
        _client().diarize(b"audio", "x.mp4", "audio/mp4")
