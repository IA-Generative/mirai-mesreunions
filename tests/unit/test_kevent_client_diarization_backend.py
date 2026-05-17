"""
Tests for the DIARIZATION_BACKEND selector (kevent vs vm-direct).

Cf docs/DIARIZATION_BACKEND.md. The toggle only affects diarization
(``diarize`` and ``diarize_async``); transcription is untouched.
"""

import importlib.util
import io
import json
import os
import sys
import types
from unittest.mock import MagicMock, patch

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


MODULE_PATH = os.path.join(
    ROOT, "services", "dmz-to-internal-bridge", "app", "kevent_client.py"
)
SPEC = importlib.util.spec_from_file_location("kc_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


# --- constructor validation ----------------------------------------------

def test_constructor_default_backend_is_kevent():
    c = MOD.KeventClient(gateway_url="https://gw", api_key="k")
    assert c.diarization_backend == "kevent"
    assert c.diarization_vm_url == ""


def test_constructor_rejects_unknown_backend():
    with pytest.raises(ValueError, match="diarization_backend"):
        MOD.KeventClient(
            gateway_url="https://gw", api_key="k",
            diarization_backend="local-pyannote",
        )


def test_vm_direct_requires_url():
    with pytest.raises(ValueError, match="diarization_vm_url"):
        MOD.KeventClient(
            gateway_url="https://gw", api_key="k",
            diarization_backend="vm-direct",
            diarization_vm_url="",
        )


def test_vm_direct_strips_trailing_slash():
    c = MOD.KeventClient(
        gateway_url="https://gw", api_key="k",
        diarization_backend="vm-direct",
        diarization_vm_url="http://1.2.3.4:8080/",
    )
    assert c.diarization_vm_url == "http://1.2.3.4:8080"


# --- dispatch: diarize() routes correctly ---------------------------------

def test_diarize_kevent_backend_hits_gateway():
    c = MOD.KeventClient(gateway_url="https://gw", api_key="k")
    _REQ.post.reset_mock()
    _REQ.post.return_value = _REQ._Resp(
        200, json_data={"segments": [], "num_speakers": 0}
    )
    result = c.diarize(b"audio", "x.m4a", "audio/m4a")
    assert result == {"segments": [], "num_speakers": 0}
    assert _REQ.post.call_count == 1
    call_url = _REQ.post.call_args[0][0]
    assert call_url == "https://gw/v1/audio/diarizations"


def test_diarize_vm_backend_calls_vm_not_gateway():
    c = MOD.KeventClient(
        gateway_url="https://gw", api_key="kevent-token",
        diarization_backend="vm-direct",
        diarization_vm_url="http://1.2.3.4:8080",
    )
    _REQ.post.reset_mock()
    fake_body = json.dumps({"segments": [{"speaker": "S1", "start": 0, "end": 1.0}]}).encode()

    fake_resp = MagicMock()
    fake_resp.read.return_value = fake_body
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda *a: None

    with patch.object(MOD.urllib.request, "urlopen", return_value=fake_resp) as urlopen:
        result = c.diarize(b"raw-audio-bytes", "meeting.m4a", "audio/m4a")

    assert result == {"segments": [{"speaker": "S1", "start": 0, "end": 1.0}]}
    assert urlopen.called
    # On a tapé la VM, pas le gateway.
    assert _REQ.post.call_count == 0
    request_arg = urlopen.call_args[0][0]
    assert request_arg.full_url == "http://1.2.3.4:8080/v1/audio/diarizations"
    # Auth: KEVENT_API_KEY réutilisée comme Authorization: Bearer.
    assert request_arg.headers["Authorization"] == "Bearer kevent-token"


def test_diarize_async_vm_backend_bypasses_polling():
    """En mode vm-direct, diarize_async ne soumet AUCUN job: appel sync direct."""
    c = MOD.KeventClient(
        gateway_url="https://gw", api_key="k",
        diarization_backend="vm-direct",
        diarization_vm_url="http://1.2.3.4:8080",
    )
    _REQ.post.reset_mock()
    fake_body = json.dumps({"segments": []}).encode()
    fake_resp = MagicMock()
    fake_resp.read.return_value = fake_body
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda *a: None

    on_submitted = MagicMock()
    on_status = MagicMock()
    with patch.object(MOD.urllib.request, "urlopen", return_value=fake_resp):
        c.diarize_async(
            b"raw", "x.m4a", "audio/m4a",
            on_submitted=on_submitted, on_status=on_status,
        )

    # Pas de submit_job vers le gateway, pas de callback.
    assert _REQ.post.call_count == 0
    on_submitted.assert_not_called()
    on_status.assert_not_called()


# --- VM error mapping -----------------------------------------------------

def _vm_client():
    return MOD.KeventClient(
        gateway_url="https://gw", api_key="k",
        diarization_backend="vm-direct",
        diarization_vm_url="http://1.2.3.4:8080",
    )


def test_vm_403_raises_auth_error():
    err = MOD.urllib.error.HTTPError(
        url="http://x", code=403, msg="Forbidden",
        hdrs=None, fp=io.BytesIO(b"nope"),
    )
    with patch.object(MOD.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(MOD.KeventAuthError):
            _vm_client().diarize(b"r", "x.m4a", "audio/m4a")


def test_vm_503_raises_transient_error():
    err = MOD.urllib.error.HTTPError(
        url="http://x", code=503, msg="Unavailable",
        hdrs=None, fp=io.BytesIO(b"down"),
    )
    with patch.object(MOD.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(MOD.KeventTransientError):
            _vm_client().diarize(b"r", "x.m4a", "audio/m4a")


def test_vm_400_raises_applicative_error():
    err = MOD.urllib.error.HTTPError(
        url="http://x", code=400, msg="Bad Request",
        hdrs=None, fp=io.BytesIO(b"bad payload"),
    )
    with patch.object(MOD.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(MOD.KeventApplicativeError):
            _vm_client().diarize(b"r", "x.m4a", "audio/m4a")


def test_vm_unreachable_raises_transient_error():
    err = MOD.urllib.error.URLError("connection refused")
    with patch.object(MOD.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(MOD.KeventTransientError):
            _vm_client().diarize(b"r", "x.m4a", "audio/m4a")


def test_vm_non_json_response_raises_applicative_error():
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"<html>not json</html>"
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda *a: None
    with patch.object(MOD.urllib.request, "urlopen", return_value=fake_resp):
        with pytest.raises(MOD.KeventApplicativeError):
            _vm_client().diarize(b"r", "x.m4a", "audio/m4a")
