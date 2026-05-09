"""
Unit tests for libs.shared.app.oidc_refresh_store.

The helper has three responsibilities used at three different points:
  - ``store_refresh_token``  : encrypt + POST to token-issuer (called by CG/admin)
  - ``fetch_ciphertext``     : GET ciphertext from token-issuer (called by file-puller)
  - ``delete_ciphertext``    : DELETE ciphertext (called by file-puller after invalid_grant)

We mock requests + secrets_crypto so the tests don't need a real Keycloak
or token-issuer running.
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


# Stub `requests` and `libs.shared.app.secrets_crypto` before loading.
def _install_stubs():
    requests_stub = types.ModuleType("requests")
    requests_stub.post = MagicMock()
    requests_stub.get = MagicMock()
    requests_stub.delete = MagicMock()

    class _RequestException(Exception):
        pass
    requests_stub.RequestException = _RequestException
    sys.modules["requests"] = requests_stub

    crypto_stub = types.ModuleType("libs.shared.app.secrets_crypto")
    crypto_stub.encrypt = MagicMock(return_value="CIPHER")
    crypto_stub.decrypt = MagicMock(return_value="PLAIN")
    sys.modules.setdefault("libs", types.ModuleType("libs"))
    sys.modules.setdefault("libs.shared", types.ModuleType("libs.shared"))
    sys.modules.setdefault("libs.shared.app", types.ModuleType("libs.shared.app"))
    sys.modules["libs.shared.app.secrets_crypto"] = crypto_stub

    return requests_stub, crypto_stub


_REQ, _CRY = _install_stubs()


MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "oidc_refresh_store.py")
SPEC = importlib.util.spec_from_file_location("oidc_refresh_store_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
MOD.__package__ = "libs.shared.app"
SPEC.loader.exec_module(MOD)


def _resp(status_code=200, json_data=None):
    r = types.SimpleNamespace()
    r.status_code = status_code
    r.json = lambda: json_data or {}
    return r


@pytest.fixture(autouse=True)
def _reset_env_and_mocks():
    os.environ["INTERNAL_API_TOKEN"] = "x" * 48
    os.environ["TOKEN_ISSUER_INTERNAL_BASE_URL"] = "http://token-issuer:8091"
    _REQ.post.reset_mock(side_effect=True, return_value=True)
    _REQ.get.reset_mock(side_effect=True, return_value=True)
    _REQ.delete.reset_mock(side_effect=True, return_value=True)
    _CRY.encrypt.reset_mock(side_effect=True, return_value=True)
    _CRY.encrypt.return_value = "CIPHER"
    yield


# --- store_refresh_token --------------------------------------------------

def test_store_returns_false_on_empty_user_sub():
    assert MOD.store_refresh_token("", "rt-123") is False
    _REQ.post.assert_not_called()


def test_store_returns_false_when_no_refresh_token():
    assert MOD.store_refresh_token("user-1", None) is False
    assert MOD.store_refresh_token("user-1", "") is False
    _REQ.post.assert_not_called()


def test_store_returns_false_when_no_internal_api_token():
    os.environ.pop("INTERNAL_API_TOKEN", None)
    assert MOD.store_refresh_token("user-1", "rt-123") is False
    _REQ.post.assert_not_called()


def test_store_encrypts_and_posts_payload():
    _REQ.post.return_value = _resp(200)
    ok = MOD.store_refresh_token(
        user_sub="user-1",
        refresh_token="rt-123",
        keycloak_iss="https://kc/realms/x",
        user_email="u@example.com",
    )
    assert ok is True
    _CRY.encrypt.assert_called_once_with("rt-123")
    args, kwargs = _REQ.post.call_args
    assert args[0].endswith("/api/v1/oidc-refresh-store")
    body = kwargs["json"]
    assert body["user_sub"] == "user-1"
    assert body["ciphertext"] == "CIPHER"
    assert body["keycloak_iss"] == "https://kc/realms/x"
    assert body["user_email"] == "u@example.com"
    assert "Bearer" in kwargs["headers"]["Authorization"]


def test_store_returns_false_on_token_issuer_5xx():
    _REQ.post.return_value = _resp(503)
    assert MOD.store_refresh_token("user-1", "rt-123") is False


def test_store_returns_false_on_network_error():
    _REQ.post.side_effect = _REQ.RequestException("conn refused")
    assert MOD.store_refresh_token("user-1", "rt-123") is False


def test_store_returns_false_when_encryption_fails():
    _CRY.encrypt.side_effect = RuntimeError("Fernet key missing")
    assert MOD.store_refresh_token("user-1", "rt-123") is False
    _REQ.post.assert_not_called()


# --- fetch_ciphertext -----------------------------------------------------

def test_fetch_returns_ciphertext_on_200():
    _REQ.get.return_value = _resp(200, json_data={"ciphertext": "CT-XYZ"})
    assert MOD.fetch_ciphertext("user-1") == "CT-XYZ"
    args, kwargs = _REQ.get.call_args
    assert args[0].endswith("/api/v1/oidc-refresh-fetch/user-1")


def test_fetch_returns_none_on_404():
    _REQ.get.return_value = _resp(404)
    assert MOD.fetch_ciphertext("user-1") is None


def test_fetch_returns_none_on_5xx():
    _REQ.get.return_value = _resp(500)
    assert MOD.fetch_ciphertext("user-1") is None


def test_fetch_returns_none_on_network_error():
    _REQ.get.side_effect = _REQ.RequestException("timeout")
    assert MOD.fetch_ciphertext("user-1") is None


def test_fetch_returns_none_on_empty_user_sub():
    assert MOD.fetch_ciphertext("") is None
    _REQ.get.assert_not_called()


# --- delete_ciphertext ----------------------------------------------------

def test_delete_returns_true_on_200():
    _REQ.delete.return_value = _resp(200)
    assert MOD.delete_ciphertext("user-1") is True
    args, _ = _REQ.delete.call_args
    assert args[0].endswith("/api/v1/oidc-refresh-delete/user-1")


def test_delete_returns_false_on_500():
    _REQ.delete.return_value = _resp(500)
    assert MOD.delete_ciphertext("user-1") is False


def test_delete_returns_false_on_network_error():
    _REQ.delete.side_effect = _REQ.RequestException("ECONNRESET")
    assert MOD.delete_ciphertext("user-1") is False
