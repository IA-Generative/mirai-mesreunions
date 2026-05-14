"""
Unit tests for services.file-mover.app.drive_client.

Same approach as test_mcr_client: stub the ``requests`` module and assert
that each HTTP failure mode maps to the right exception class so the
caller (meeting-prep route) can react uniformly (wipe token vs. retry vs.
surface-to-user).
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
        def __init__(self, status_code=200, json_data=None, text="", content=b"", headers=None):
            self.status_code = status_code
            self._json = json_data
            self.text = text
            self.content = content
            self.headers = headers or {}

        def json(self):
            if isinstance(self._json, Exception):
                raise self._json
            if self._json is None:
                raise ValueError("no json")
            return self._json

    class _RequestException(Exception):
        pass

    requests_stub.RequestException = _RequestException
    requests_stub.get = MagicMock()
    requests_stub.post = MagicMock()
    requests_stub._Resp = _Resp
    sys.modules["requests"] = requests_stub
    return requests_stub


_REQ = _install_requests_stub()


def _resp(status_code=200, json_data=None, text="", content=b"", headers=None):
    return _REQ._Resp(
        status_code=status_code, json_data=json_data, text=text, content=content, headers=headers
    )


MODULE_PATH = os.path.join(ROOT, "services", "file-mover", "app", "drive_client.py")
SPEC = importlib.util.spec_from_file_location("drive_client_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)


def _client():
    return MOD.DriveClient(
        base_url="https://mesfichiers.example.com",
        oidc_token_endpoint="https://kc.example.com/realms/x/protocol/openid-connect/token",
        oidc_client_id="audio-upload",
        oidc_client_secret="secret",
    )


@pytest.fixture(autouse=True)
def _reset_mocks():
    _REQ.get.reset_mock(side_effect=True, return_value=True)
    _REQ.post.reset_mock(side_effect=True, return_value=True)


# --- Constructor ---------------------------------------------------------

def test_constructor_rejects_missing_base_url():
    with pytest.raises(ValueError):
        MOD.DriveClient(base_url="", oidc_token_endpoint="https://kc", oidc_client_id="x")


def test_constructor_rejects_missing_token_endpoint():
    with pytest.raises(ValueError):
        MOD.DriveClient(base_url="https://d", oidc_token_endpoint="", oidc_client_id="x")


def test_constructor_strips_trailing_slash():
    c = MOD.DriveClient(
        base_url="https://d/",
        oidc_token_endpoint="https://kc",
        oidc_client_id="x",
    )
    assert c.base_url == "https://d"


# --- exchange_refresh ----------------------------------------------------

def test_exchange_refresh_success():
    _REQ.post.return_value = _resp(200, json_data={"access_token": "AT-1"})
    assert _client().exchange_refresh("rt-1") == "AT-1"


def test_exchange_refresh_400_raises_auth_error():
    _REQ.post.return_value = _resp(400, text='{"error":"invalid_grant"}')
    with pytest.raises(MOD.DriveAuthError):
        _client().exchange_refresh("rt-expired")


def test_exchange_refresh_5xx_raises_transient_error():
    _REQ.post.return_value = _resp(503, text="kc down")
    with pytest.raises(MOD.DriveTransientError):
        _client().exchange_refresh("rt-1")


def test_exchange_refresh_403_raises_applicative_error():
    _REQ.post.return_value = _resp(403, text="forbidden")
    with pytest.raises(MOD.DriveApplicativeError):
        _client().exchange_refresh("rt-1")


def test_exchange_refresh_network_failure_raises_transient_error():
    _REQ.post.side_effect = _REQ.RequestException("connection refused")
    with pytest.raises(MOD.DriveTransientError):
        _client().exchange_refresh("rt-1")


def test_exchange_refresh_empty_token_raises_auth_error():
    with pytest.raises(MOD.DriveAuthError):
        _client().exchange_refresh("")


def test_exchange_refresh_response_without_access_token_raises_auth_error():
    _REQ.post.return_value = _resp(200, json_data={"foo": "bar"})
    with pytest.raises(MOD.DriveAuthError):
        _client().exchange_refresh("rt-1")


# --- get_item ------------------------------------------------------------

def test_get_item_success_returns_dict():
    _REQ.get.return_value = _resp(200, json_data={"id": "abc", "title": "Doc"})
    out = _client().get_item("AT", "abc")
    assert out == {"id": "abc", "title": "Doc"}


def test_get_item_404_raises_applicative_error():
    _REQ.get.return_value = _resp(404, text="not found")
    with pytest.raises(MOD.DriveApplicativeError):
        _client().get_item("AT", "missing")


def test_get_item_401_raises_auth_error():
    _REQ.get.return_value = _resp(401, text="unauthorized")
    with pytest.raises(MOD.DriveAuthError):
        _client().get_item("AT", "x")


def test_get_item_500_raises_transient_error():
    _REQ.get.return_value = _resp(500, text="oops")
    with pytest.raises(MOD.DriveTransientError):
        _client().get_item("AT", "x")


def test_get_item_non_dict_response_raises_applicative_error():
    _REQ.get.return_value = _resp(200, json_data=["not", "a", "dict"])
    with pytest.raises(MOD.DriveApplicativeError):
        _client().get_item("AT", "x")


def test_get_item_passes_bearer_header():
    _REQ.get.return_value = _resp(200, json_data={"id": "x"})
    _client().get_item("my-access-token", "x")
    call_kwargs = _REQ.get.call_args.kwargs
    assert call_kwargs["headers"]["Authorization"] == "Bearer my-access-token"


# --- list_children -------------------------------------------------------

def test_list_children_handles_plain_array():
    _REQ.get.return_value = _resp(200, json_data=[{"id": "1"}, {"id": "2"}])
    out = _client().list_children("AT", "folder-x")
    assert [it["id"] for it in out] == ["1", "2"]


def test_list_children_handles_paginated_envelope():
    _REQ.get.return_value = _resp(200, json_data={"count": 2, "results": [{"id": "1"}, {"id": "2"}]})
    out = _client().list_children("AT", "folder-x")
    assert [it["id"] for it in out] == ["1", "2"]


def test_list_children_filters_non_dict_entries():
    _REQ.get.return_value = _resp(200, json_data=[{"id": "1"}, "not-a-dict", None, {"id": "2"}])
    out = _client().list_children("AT", "folder-x")
    assert [it["id"] for it in out] == ["1", "2"]


def test_list_children_403_raises_applicative_error():
    """403 from Drive on children means the user doesn't own this folder."""
    _REQ.get.return_value = _resp(403, text="not yours")
    with pytest.raises(MOD.DriveAuthError):
        _client().list_children("AT", "folder-x")


def test_list_children_invalid_shape_raises_applicative_error():
    _REQ.get.return_value = _resp(200, json_data={"unexpected": "object"})
    with pytest.raises(MOD.DriveApplicativeError):
        _client().list_children("AT", "folder-x")


# --- download_item -------------------------------------------------------

def test_download_item_uses_metadata_download_url_when_present():
    """When metadata exposes a presigned URL on a different host, no bearer is sent on the download."""
    metadata = {"id": "x", "download_url": "https://s3.example.com/bucket/file?sig=abc"}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        if url.endswith("/items/x/"):
            return _resp(200, json_data=metadata)
        # download leg: must not carry Authorization
        assert headers == {} or "Authorization" not in (headers or {})
        return _resp(200, content=b"file-bytes", headers={"Content-Type": "application/pdf"})

    _REQ.get.side_effect = fake_get
    content, ct = _client().download_item("AT", "x")
    assert content == b"file-bytes"
    assert ct == "application/pdf"


def test_download_item_falls_back_to_download_endpoint_when_no_url_in_metadata():
    """No URL in metadata → GET /items/{id}/download/ with bearer."""
    calls = []

    def fake_get(url, headers=None, timeout=None, **kwargs):
        calls.append(url)
        if url.endswith("/items/x/"):
            return _resp(200, json_data={"id": "x", "title": "noop"})
        return _resp(200, content=b"data", headers={"Content-Type": "text/plain"})

    _REQ.get.side_effect = fake_get
    content, ct = _client().download_item("AT", "x")
    assert content == b"data"
    assert ct == "text/plain"
    assert any(url.endswith("/items/x/download/") for url in calls)


def test_download_item_keeps_bearer_for_same_host_download_url():
    """If metadata URL is on the Drive itself, the bearer must accompany it."""
    seen_headers = {}

    def fake_get(url, headers=None, timeout=None, **kwargs):
        if url.endswith("/items/x/"):
            return _resp(200, json_data={"id": "x", "url": "https://mesfichiers.example.com/media/abc"})
        seen_headers.update(headers or {})
        return _resp(200, content=b"data")

    _REQ.get.side_effect = fake_get
    _client().download_item("AT", "x")
    assert seen_headers.get("Authorization") == "Bearer AT"


def test_download_item_max_bytes_enforced():
    def fake_get(url, headers=None, timeout=None, **kwargs):
        if url.endswith("/items/x/"):
            return _resp(200, json_data={"id": "x"})
        return _resp(200, content=b"x" * 1000)

    _REQ.get.side_effect = fake_get
    with pytest.raises(MOD.DriveApplicativeError):
        _client().download_item("AT", "x", max_bytes=100)


def test_download_item_401_on_download_raises_auth_error():
    def fake_get(url, headers=None, timeout=None, **kwargs):
        if url.endswith("/items/x/"):
            return _resp(200, json_data={"id": "x"})
        return _resp(401, text="expired")

    _REQ.get.side_effect = fake_get
    with pytest.raises(MOD.DriveAuthError):
        _client().download_item("AT", "x")


def test_download_item_5xx_on_download_raises_transient_error():
    def fake_get(url, headers=None, timeout=None, **kwargs):
        if url.endswith("/items/x/"):
            return _resp(200, json_data={"id": "x"})
        return _resp(503, text="upstream down")

    _REQ.get.side_effect = fake_get
    with pytest.raises(MOD.DriveTransientError):
        _client().download_item("AT", "x")


def test_download_item_uses_metadata_mime_type_when_header_missing():
    def fake_get(url, headers=None, timeout=None, **kwargs):
        if url.endswith("/items/x/"):
            return _resp(200, json_data={"id": "x", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
        return _resp(200, content=b"data", headers={})  # no Content-Type

    _REQ.get.side_effect = fake_get
    _, ct = _client().download_item("AT", "x")
    assert ct == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
