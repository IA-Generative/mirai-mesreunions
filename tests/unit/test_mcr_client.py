"""
Unit tests for services.dmz-to-internal-bridge.app.mcr_client.

Mock the underlying ``requests`` calls and assert that each HTTP failure
mode maps to the right exception class — that's how the internal-ingester chooses
between "wipe token + mark mcr_auth_failed" (no retry), "mark mcr_rejected"
(no retry) and "raise so queue retries" (transient).
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


# Stub `requests` before loading mcr_client.
_responses_returned = {}


def _install_requests_stub():
    requests_stub = types.ModuleType("requests")

    class _Resp:
        def __init__(self, status_code=200, json_data=None, text=""):
            self.status_code = status_code
            self._json = json_data or {}
            self.text = text

        def json(self):
            if isinstance(self._json, Exception):
                raise self._json
            return self._json

    class _RequestException(Exception):
        pass

    requests_stub.RequestException = _RequestException
    requests_stub.HTTPError = _RequestException
    requests_stub.post = MagicMock()
    requests_stub.put = MagicMock()
    requests_stub.get = MagicMock()
    requests_stub._Resp = _Resp
    sys.modules["requests"] = requests_stub
    return requests_stub


_REQ = _install_requests_stub()


def _resp(status_code=200, json_data=None, text=""):
    return _REQ._Resp(status_code=status_code, json_data=json_data, text=text)


# Ensure libs.shared.app is loaded as a real package BEFORE mcr_client tries
# to ``from libs.shared.app.mirai_oidc import ...``. Other tests in the suite
# may have registered ``libs.shared.app`` in sys.modules as a non-package via
# importlib.spec_from_file_location, which would cause the from-import to
# fail with "'libs.shared.app' is not a package". Forcing a fresh import here
# is cheap and order-independent.
for _stale in [k for k in list(sys.modules) if k == "libs" or k.startswith("libs.")]:
    del sys.modules[_stale]
import libs.shared.app.mirai_oidc  # noqa: F401  (just to populate sys.modules)

# Now load mcr_client
MODULE_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "mcr_client.py")
SPEC = importlib.util.spec_from_file_location("mcr_client_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def _client():
    return MOD.MCRClient(
        gateway_url="https://mcr.example.com",
        oidc_token_endpoint="https://kc.example.com/realms/x/protocol/openid-connect/token",
        oidc_client_id="audio-upload",
        oidc_client_secret="secret",
    )


@pytest.fixture(autouse=True)
def _reset_mocks():
    _REQ.post.reset_mock(side_effect=True, return_value=True)
    _REQ.put.reset_mock(side_effect=True, return_value=True)
    _REQ.get.reset_mock(side_effect=True, return_value=True)


# --- Constructor ----------------------------------------------------------

def test_constructor_rejects_missing_gateway_url():
    with pytest.raises(ValueError):
        MOD.MCRClient(gateway_url="", oidc_token_endpoint="https://kc",
                      oidc_client_id="x")


def test_constructor_rejects_missing_token_endpoint():
    with pytest.raises(ValueError):
        MOD.MCRClient(gateway_url="https://mcr", oidc_token_endpoint="",
                      oidc_client_id="x")


# --- exchange_refresh -----------------------------------------------------

def test_exchange_refresh_success_returns_access_token():
    _REQ.post.return_value = _resp(200, json_data={"access_token": "AT-123"})
    assert _client().exchange_refresh("rt-123") == "AT-123"


def test_exchange_refresh_400_raises_auth_error():
    _REQ.post.return_value = _resp(400, json_data={"error": "invalid_grant"},
                                   text='{"error":"invalid_grant"}')
    with pytest.raises(MOD.MCRAuthError):
        _client().exchange_refresh("rt-expired")


def test_exchange_refresh_5xx_raises_transient_error():
    _REQ.post.return_value = _resp(503, text="kc down")
    with pytest.raises(MOD.MCRTransientError):
        _client().exchange_refresh("rt-123")


def test_exchange_refresh_403_raises_applicative_error():
    _REQ.post.return_value = _resp(403, text="Forbidden")
    with pytest.raises(MOD.MCRApplicativeError):
        _client().exchange_refresh("rt-123")


def test_exchange_refresh_network_failure_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("connection refused")
    with pytest.raises(MOD.MCRTransientError):
        _client().exchange_refresh("rt-123")


def test_exchange_refresh_empty_token_raises_auth_error():
    """Defensive: caller passing empty refresh shouldn't reach the network."""
    with pytest.raises(MOD.MCRAuthError):
        _client().exchange_refresh("")


def test_exchange_refresh_response_without_access_token_raises_auth_error():
    _REQ.post.return_value = _resp(200, json_data={"foo": "bar"})
    with pytest.raises(MOD.MCRAuthError):
        _client().exchange_refresh("rt-123")


# --- create_meeting ------------------------------------------------------

def test_create_meeting_success_returns_id():
    _REQ.post.return_value = _resp(200, json_data={"meeting_id": "m-42"})
    assert _client().create_meeting("AT", {"name": "x"}) == "m-42"


def test_create_meeting_accepts_id_field_as_fallback():
    _REQ.post.return_value = _resp(200, json_data={"id": "m-42"})
    assert _client().create_meeting("AT", {"name": "x"}) == "m-42"


def test_create_meeting_401_raises_auth_error():
    _REQ.post.return_value = _resp(401, text="Unauthorized")
    with pytest.raises(MOD.MCRAuthError):
        _client().create_meeting("AT", {"name": "x"})


def test_create_meeting_400_raises_applicative_error():
    _REQ.post.return_value = _resp(400, text="bad payload")
    with pytest.raises(MOD.MCRApplicativeError):
        _client().create_meeting("AT", {"name": "x"})


def test_create_meeting_500_raises_transient_error():
    _REQ.post.return_value = _resp(500, text="oops")
    with pytest.raises(MOD.MCRTransientError):
        _client().create_meeting("AT", {"name": "x"})


def test_create_meeting_response_missing_id_raises_applicative_error():
    _REQ.post.return_value = _resp(200, json_data={"unrelated": "x"})
    with pytest.raises(MOD.MCRApplicativeError):
        _client().create_meeting("AT", {"name": "x"})


# --- generate_presigned --------------------------------------------------

def test_generate_presigned_success():
    _REQ.post.return_value = _resp(200, json_data={"presigned_url": "https://signed/"})
    assert _client().generate_presigned("AT", "m-1", "x.mp4") == "https://signed/"


def test_generate_presigned_camel_case_field_accepted():
    _REQ.post.return_value = _resp(200, json_data={"presignedUrl": "https://signed/"})
    assert _client().generate_presigned("AT", "m-1", "x.mp4") == "https://signed/"


def test_generate_presigned_404_raises_applicative_error():
    _REQ.post.return_value = _resp(404, text="meeting not found")
    with pytest.raises(MOD.MCRApplicativeError):
        _client().generate_presigned("AT", "missing", "x.mp4")


# --- upload_binary -------------------------------------------------------

def test_upload_binary_200_succeeds_silently():
    _REQ.put.return_value = _resp(200)
    _client().upload_binary("https://signed/", b"audio-data", "audio/mp4")  # no raise


def test_upload_binary_403_raises_applicative_error():
    _REQ.put.return_value = _resp(403, text="forbidden — presigned expired")
    with pytest.raises(MOD.MCRApplicativeError):
        _client().upload_binary("https://signed/", b"audio-data", "audio/mp4")


def test_upload_binary_500_raises_transient_error():
    _REQ.put.return_value = _resp(500)
    with pytest.raises(MOD.MCRTransientError):
        _client().upload_binary("https://signed/", b"audio-data", "audio/mp4")


def test_upload_binary_network_failure_raises_transient_error():
    _REQ.put.side_effect = _REQ.RequestException("conn reset")
    with pytest.raises(MOD.MCRTransientError):
        _client().upload_binary("https://signed/", b"audio-data", "audio/mp4")


# --- Pull: list_meetings -------------------------------------------------

def test_list_meetings_200_returns_paginated_dict():
    payload = {"total_items": 2, "total_pages": 1, "page": 1,
               "data": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}
    _REQ.get.return_value = _resp(200, json_data=payload)
    out = _client().list_meetings("AT", page=1, page_size=10)
    assert out == payload
    # URL was the gateway + /api/meetings (no trailing slash — MCR redirects
    # trailing-slash to non-slash but downgrades to http:// which the CNP
    # frontend-egress blocks; cf bug 2026-05-23 prod-bêta first deploy).
    args, kwargs = _REQ.get.call_args
    assert args[0].endswith("/api/meetings")
    assert kwargs["params"] == {"page": 1, "page_size": 10}
    assert kwargs["headers"]["Authorization"] == "Bearer AT"


def test_list_meetings_with_search_propagates_param():
    _REQ.get.return_value = _resp(200, json_data={"data": []})
    _client().list_meetings("AT", page=2, page_size=20, search="kevin")
    _, kwargs = _REQ.get.call_args
    assert kwargs["params"]["search"] == "kevin"
    assert kwargs["params"]["page"] == 2


def test_list_meetings_401_raises_auth_error():
    _REQ.get.return_value = _resp(401, text="invalid token")
    with pytest.raises(MOD.MCRAuthError):
        _client().list_meetings("AT")


def test_list_meetings_500_raises_transient_error():
    _REQ.get.return_value = _resp(500)
    with pytest.raises(MOD.MCRTransientError):
        _client().list_meetings("AT")


def test_list_meetings_network_failure_raises_transient_error():
    _REQ.get.side_effect = _REQ.RequestException("conn reset")
    with pytest.raises(MOD.MCRTransientError):
        _client().list_meetings("AT")


# --- Pull: download_audio ------------------------------------------------

def test_download_audio_200_returns_response_unchanged():
    resp = _resp(200)
    _REQ.get.return_value = resp
    out = _client().download_audio("AT", "42")
    assert out is resp
    args, kwargs = _REQ.get.call_args
    assert args[0].endswith("/api/meetings/42/audio")
    assert kwargs["stream"] is True


def test_download_audio_404_raises_applicative_error_with_clear_msg():
    # close() should be called even on the missing branch (cleanup)
    class _R(_REQ._Resp):
        def __init__(self):
            super().__init__(status_code=404)
            self.closed = False

        def close(self):
            self.closed = True
    resp = _R()
    _REQ.get.return_value = resp
    with pytest.raises(MOD.MCRApplicativeError) as exc:
        _client().download_audio("AT", "missing")
    assert "No audio available" in str(exc.value)
    assert resp.closed is True


def test_download_audio_403_raises_auth_error():
    _REQ.get.return_value = _resp(403)
    with pytest.raises(MOD.MCRAuthError):
        _client().download_audio("AT", "42")


def test_download_audio_500_raises_transient_error():
    _REQ.get.return_value = _resp(500)
    with pytest.raises(MOD.MCRTransientError):
        _client().download_audio("AT", "42")


# --- Pull: download_transcription_docx -----------------------------------

def test_download_transcription_docx_200_returns_bytes():
    fake = _resp(200)
    fake.content = b"PK\x03\x04 fake docx body"
    _REQ.post.return_value = fake
    out = _client().download_transcription_docx("AT", "42")
    assert out == b"PK\x03\x04 fake docx body"
    args, kwargs = _REQ.post.call_args
    assert args[0].endswith("/api/meetings/42/transcription")
    assert kwargs["headers"]["Authorization"] == "Bearer AT"


def test_download_transcription_docx_404_raises_applicative():
    _REQ.post.return_value = _resp(404)
    with pytest.raises(MOD.MCRApplicativeError) as exc:
        _client().download_transcription_docx("AT", "missing")
    assert "No transcription available" in str(exc.value)


def test_download_transcription_docx_410_raises_applicative():
    _REQ.post.return_value = _resp(410)
    with pytest.raises(MOD.MCRApplicativeError):
        _client().download_transcription_docx("AT", "missing")


def test_download_transcription_docx_500_raises_transient_error():
    _REQ.post.return_value = _resp(500)
    with pytest.raises(MOD.MCRTransientError):
        _client().download_transcription_docx("AT", "42")
