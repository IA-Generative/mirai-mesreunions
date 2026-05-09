"""
Unit tests for services.file-mover.app.mcr_client.

Mock the underlying ``requests`` calls and assert that each HTTP failure
mode maps to the right exception class — that's how the file-puller chooses
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
    requests_stub._Resp = _Resp
    sys.modules["requests"] = requests_stub
    return requests_stub


_REQ = _install_requests_stub()


def _resp(status_code=200, json_data=None, text=""):
    return _REQ._Resp(status_code=status_code, json_data=json_data, text=text)


# Now load mcr_client
MODULE_PATH = os.path.join(ROOT, "services", "file-mover", "app", "mcr_client.py")
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
