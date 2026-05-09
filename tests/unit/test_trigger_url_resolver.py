"""
Unit tests for libs.shared.app.trigger_url.resolved_trigger_url.

The function turns a raw env-var value into either a usable HTTP(S) URL or
None. Its primary security responsibility is to refuse anything that isn't
http/https with a real host — that's what stops a typo or sentinel word
from making file-mover open `file:///etc/passwd` or similar.
"""

import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "trigger_url.py")
SPEC = importlib.util.spec_from_file_location("trigger_url", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)

resolved_trigger_url = MOD.resolved_trigger_url


# --- Disable cases ---------------------------------------------------------

def test_none_disables():
    assert resolved_trigger_url(None) is None


def test_empty_string_disables():
    assert resolved_trigger_url("") is None


def test_whitespace_only_disables():
    assert resolved_trigger_url("   \t\n  ") is None


def test_sentinel_words_disable():
    """Anything that isn't a parsable URL with http(s) scheme must be off."""
    for sentinel in ("false", "FALSE", "off", "deactivate", "no", "0", "disabled", "none"):
        assert resolved_trigger_url(sentinel) is None, sentinel


def test_arbitrary_token_without_scheme_disables():
    """Bare hostnames or random strings don't count."""
    for raw in ("not-a-url", "hostname", "example.com", "/path", "://"):
        assert resolved_trigger_url(raw) is None, raw


# --- Security: reject non-HTTP schemes ------------------------------------

def test_file_scheme_disables():
    """file:// must never be accepted — protects against accidental local reads."""
    assert resolved_trigger_url("file:///etc/passwd") is None


def test_ftp_scheme_disables():
    assert resolved_trigger_url("ftp://example.com/some/path") is None


def test_mailto_scheme_disables():
    assert resolved_trigger_url("mailto:admin@example.com") is None


def test_javascript_scheme_disables():
    assert resolved_trigger_url("javascript:alert(1)") is None


def test_data_scheme_disables():
    assert resolved_trigger_url("data:text/plain,hello") is None


def test_scheme_only_no_host_disables():
    """An http scheme without a host is unusable."""
    assert resolved_trigger_url("http://") is None
    assert resolved_trigger_url("https://") is None


# --- Activation cases -----------------------------------------------------

def test_simple_http_url_enables():
    assert resolved_trigger_url("http://example.com") == "http://example.com"


def test_simple_https_url_enables():
    assert resolved_trigger_url("https://example.com") == "https://example.com"


def test_full_pull_trigger_url_enables():
    url = "https://pull-trigger.fake-domain.name/api/v1/pull-trigger"
    assert resolved_trigger_url(url) == url


def test_url_with_port_enables():
    assert resolved_trigger_url("https://host:8443/path") == "https://host:8443/path"


def test_url_with_query_string_enables():
    url = "https://host/api?token=abc&debug=1"
    assert resolved_trigger_url(url) == url


def test_url_with_path_only_enables():
    assert resolved_trigger_url("http://localhost:5000/") == "http://localhost:5000/"


# --- Robustness ------------------------------------------------------------

def test_surrounding_whitespace_is_stripped():
    """Operator might paste with trailing newlines; tolerate it."""
    assert resolved_trigger_url("  https://example.com  ") == "https://example.com"
    assert resolved_trigger_url("\thttps://x/y\n") == "https://x/y"


def test_uppercase_scheme_accepted():
    """RFC says scheme is case-insensitive; urlparse already lowercases it."""
    # Note: we lowercase only for comparison, the returned URL preserves the
    # original casing so upstream signature checks aren't broken.
    assert resolved_trigger_url("HTTPS://Example.com/") == "HTTPS://Example.com/"


def test_returned_value_is_safe_to_pass_to_requests():
    """Sanity: anything we return should at least look like a URL."""
    out = resolved_trigger_url("https://x.example/api/v1/pull-trigger")
    assert isinstance(out, str)
    assert out.startswith("https://")
    assert "://" in out
