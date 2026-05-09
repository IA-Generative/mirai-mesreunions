"""
Unit tests for the LiteLLM-compatible chat client wrapper.
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


MODULE_PATH = os.path.join(ROOT, "services", "file-mover", "app", "llm_client.py")
SPEC = importlib.util.spec_from_file_location("llm_client_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def _client():
    return MOD.LLMClient(base_url="https://llm.example.com", api_key="sk-test")


@pytest.fixture(autouse=True)
def _reset():
    _REQ.post.reset_mock(side_effect=True, return_value=True)


# --- constructor ----------------------------------------------------------

def test_constructor_rejects_empty_url():
    with pytest.raises(ValueError):
        MOD.LLMClient(base_url="", api_key="x")


def test_constructor_rejects_empty_key():
    with pytest.raises(ValueError):
        MOD.LLMClient(base_url="https://x", api_key="")


# --- chat (plain text) ----------------------------------------------------

def test_chat_returns_assistant_content():
    _REQ.post.return_value = _resp(200, json_data={
        "choices": [{"message": {"content": "Bonjour."}}]
    })
    out = _client().chat("model-x", [{"role": "user", "content": "salut"}])
    assert out == "Bonjour."
    args, kwargs = _REQ.post.call_args
    assert args[0].endswith("/v1/chat/completions")
    assert kwargs["json"]["model"] == "model-x"


def test_chat_uses_authorization_header():
    _REQ.post.return_value = _resp(200, json_data={"choices": [{"message": {"content": "x"}}]})
    _client().chat("m", [])
    _, kwargs = _REQ.post.call_args
    assert kwargs["headers"]["Authorization"].startswith("Bearer sk-")


def test_chat_passes_response_format_when_provided():
    _REQ.post.return_value = _resp(200, json_data={"choices": [{"message": {"content": "{}"}}]})
    _client().chat("m", [], response_format={"type": "json_object"})
    _, kwargs = _REQ.post.call_args
    assert kwargs["json"]["response_format"] == {"type": "json_object"}


def test_chat_401_raises_auth_error():
    _REQ.post.return_value = _resp(401, text="invalid api key")
    with pytest.raises(MOD.LLMAuthError):
        _client().chat("m", [])


def test_chat_5xx_raises_transient_error():
    _REQ.post.return_value = _resp(503)
    with pytest.raises(MOD.LLMTransientError):
        _client().chat("m", [])


def test_chat_400_raises_applicative_error():
    _REQ.post.return_value = _resp(400, text="context length exceeded")
    with pytest.raises(MOD.LLMApplicativeError):
        _client().chat("m", [])


def test_chat_empty_choices_raises_applicative_error():
    _REQ.post.return_value = _resp(200, json_data={"choices": []})
    with pytest.raises(MOD.LLMApplicativeError):
        _client().chat("m", [])


def test_chat_network_failure_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("conn reset")
    with pytest.raises(MOD.LLMTransientError):
        _client().chat("m", [])


# --- chat_json ------------------------------------------------------------

def test_chat_json_parses_valid_json():
    _REQ.post.return_value = _resp(200, json_data={
        "choices": [{"message": {"content": '{"name": "Jean", "id": 7}'}}]
    })
    out = _client().chat_json("m", [])
    assert out == {"name": "Jean", "id": 7}


def test_chat_json_invalid_json_raises_applicative_error():
    _REQ.post.return_value = _resp(200, json_data={
        "choices": [{"message": {"content": "Sure! Here is the JSON: ..."}}]
    })
    with pytest.raises(MOD.LLMApplicativeError):
        _client().chat_json("m", [])


def test_chat_json_sets_response_format():
    """chat_json must request structured output."""
    _REQ.post.return_value = _resp(200, json_data={"choices": [{"message": {"content": "{}"}}]})
    _client().chat_json("m", [])
    _, kwargs = _REQ.post.call_args
    assert kwargs["json"]["response_format"] == {"type": "json_object"}
