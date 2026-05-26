"""Unit tests for libs.shared.app.mirai_oidc.

The helper is a thin wrapper around ``requests.post`` to the Keycloak token
endpoint, with explicit error families used by both mesreunions-web (sync)
and internal-ingester (async). We stub ``requests`` so the tests don't
need a real KC.
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
            self._json = json_data or {}
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


MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "mirai_oidc.py")
SPEC = importlib.util.spec_from_file_location("mirai_oidc_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)


@pytest.fixture(autouse=True)
def _reset_mocks():
    _REQ.post.reset_mock(side_effect=True, return_value=True)


def _call(**overrides):
    kwargs = dict(
        token_endpoint="https://kc.example.com/realms/mirai/protocol/openid-connect/token",
        client_id="mes-reunions",
        refresh_token="rt-123",
        client_secret="secret",
    )
    kwargs.update(overrides)
    return MOD.exchange_refresh_token(**kwargs)


# ─── Success path ────────────────────────────────────────────────────────

def test_returns_access_token_on_200():
    _REQ.post.return_value = _resp(200, json_data={"access_token": "AT-ok"})
    assert _call() == "AT-ok"


def test_sends_grant_type_refresh_token_and_client_id():
    _REQ.post.return_value = _resp(200, json_data={"access_token": "AT"})
    _call()
    args, kwargs = _REQ.post.call_args
    data = kwargs["data"]
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "rt-123"
    assert data["client_id"] == "mes-reunions"
    assert data["client_secret"] == "secret"


def test_omits_client_secret_when_not_provided():
    _REQ.post.return_value = _resp(200, json_data={"access_token": "AT"})
    _call(client_secret="")
    args, kwargs = _REQ.post.call_args
    assert "client_secret" not in kwargs["data"]


# ─── Error families ──────────────────────────────────────────────────────

def test_empty_refresh_raises_auth_error_without_network_call():
    with pytest.raises(MOD.OIDCAuthError):
        _call(refresh_token="")
    assert _REQ.post.call_count == 0


def test_400_invalid_grant_raises_auth_error():
    _REQ.post.return_value = _resp(400, text='{"error":"invalid_grant"}')
    with pytest.raises(MOD.OIDCAuthError):
        _call()


def test_403_raises_applicative_error():
    _REQ.post.return_value = _resp(403, text="forbidden")
    with pytest.raises(MOD.OIDCApplicativeError):
        _call()


def test_500_raises_transient_error():
    _REQ.post.return_value = _resp(500)
    with pytest.raises(MOD.OIDCTransientError):
        _call()


def test_network_failure_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("conn reset")
    with pytest.raises(MOD.OIDCTransientError):
        _call()


def test_response_without_access_token_raises_auth_error():
    _REQ.post.return_value = _resp(200, json_data={"foo": "bar"})
    with pytest.raises(MOD.OIDCAuthError):
        _call()


def test_response_not_json_raises_transient_error():
    _REQ.post.return_value = _resp(200, json_data=ValueError("not json"))
    with pytest.raises(MOD.OIDCTransientError):
        _call()


# ─── Argument validation ────────────────────────────────────────────────

def test_missing_token_endpoint_raises_value_error():
    with pytest.raises(ValueError):
        _call(token_endpoint="")


def test_missing_client_id_raises_value_error():
    with pytest.raises(ValueError):
        _call(client_id="")
