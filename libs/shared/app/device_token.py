"""Signed device token helpers (stateless fast-path verification)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_device_token(payload: Dict[str, Any], secret: str) -> str:
    """Create HMAC-SHA256 signed token.

    Format: <base64url(json_payload)>.<base64url(signature)>
    """
    raw_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).digest()
    return f"{_b64url_encode(raw_payload)}.{_b64url_encode(sig)}"


def verify_device_token(token: str, secret: str) -> Dict[str, Any]:
    """Verify token signature and return payload dict.

    Raises ValueError on invalid format/signature/payload.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        raw_payload = _b64url_decode(payload_b64)
        got_sig = _b64url_decode(sig_b64)
    except Exception as exc:
        raise ValueError("invalid_token_format") from exc

    expected = hmac.new(secret.encode("utf-8"), raw_payload, hashlib.sha256).digest()
    if not hmac.compare_digest(got_sig, expected):
        raise ValueError("invalid_token_signature")

    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid_token_payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_token_payload")
    return payload


def utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

