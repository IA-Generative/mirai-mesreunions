"""Shared security helpers for internal API authentication."""

import hmac
import os
from typing import Optional


WEAK_MARKERS = (
    "changeme",
    "change-me",
    "default",
    "example",
    "dev-",
    "test-",
    "dummy",
)


def parse_bearer_token(header_value: str) -> Optional[str]:
    """Parse a standard Authorization Bearer header."""
    if not header_value or not header_value.startswith("Bearer "):
        return None
    token = header_value.split(" ", 1)[1].strip()
    return token or None


def verify_bearer_token(header_value: str, expected_token: str) -> bool:
    """Constant-time Bearer token verification."""
    token = parse_bearer_token(header_value)
    if not token or not expected_token:
        return False
    return hmac.compare_digest(token, expected_token)


def is_strong_shared_secret(value: str) -> bool:
    """Minimal policy for internal shared secrets used across services."""
    if not value or len(value) < 32:
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in WEAK_MARKERS):
        return False
    return True


def require_strong_shared_secret(env_key: str) -> str:
    """
    Ensure a strong secret exists in env.
    Raises RuntimeError if missing/weak to fail fast at startup.
    """
    value = os.getenv(env_key, "")
    if not is_strong_shared_secret(value):
        raise RuntimeError(
            f"{env_key} is missing or too weak. "
            "Use at least 32 chars and avoid placeholders/default/test values."
        )
    return value

