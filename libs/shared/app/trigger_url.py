"""
URL resolver for the optional cross-cluster pull trigger.

The dmz-to-internal-bridge (DMZ) tries to wake the internal-ingester (protected zone) up the
moment a new file is ready, by POSTing to the pull-trigger HTTP endpoint
exposed via Ingress. The activation of that wake-up path is controlled by
a single env var, ``INTERNAL_PUSH_TRIGGER_URL``: the variable's value *is*
the switch — there is no separate boolean flag.

If the value parses as an HTTP(S) URL with a non-empty hostname, the trigger
is enabled. Anything else — empty string, sentinel words like ``"false"`` /
``"deactivate"`` / ``"off"``, malformed URL, non-HTTP scheme — disables it.
This means an operator can pause the trigger by writing any non-URL token
(or just leaving the env unset), without having to remember a second var.

The defensive scheme allowlist (only ``http`` / ``https``) intentionally
rejects ``file://``, ``ftp://``, ``mailto:``, ``javascript:`` and friends
so that a misconfigured value cannot make ``requests.post`` reach for a
local file or some unexpected protocol.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

_VALID_SCHEMES = {"http", "https"}


def resolved_trigger_url(raw: Optional[str]) -> Optional[str]:
    """
    Return the cleaned URL when ``raw`` is a usable HTTP(S) URL, else None.

    The returned value is the input with surrounding whitespace stripped —
    callers can pass it straight to ``requests.post``.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        parsed = urlparse(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in _VALID_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    return candidate
