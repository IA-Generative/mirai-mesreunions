"""
Server-side fingerprint normalization for browser↔PWA fusion.

The mobile-upload-pwa client sends a 6-field fingerprint string:
    user_agent | platform | language | screen_width | screen_height | timezone

We hash only the fields that stay stable when the same physical device launches
the page in browser mode and again as an installed PWA: platform, language,
timezone. user_agent often gains a "wv" / "Standalone" marker, and the screen
dimensions can change with the status bar, so they are intentionally dropped.

Cross-user collisions are not a concern because every fusion lookup is scoped
to a given qr_token (one user, one session).
"""

from __future__ import annotations

import hashlib


def compute_fp_hash(device_fingerprint: str) -> str:
    """Return a hex SHA-256 of the normalized fingerprint, or '' if empty."""
    if not device_fingerprint:
        return ""
    parts = device_fingerprint.split("|")
    platform = parts[1].strip().lower() if len(parts) > 1 else ""
    language = parts[2].strip().lower() if len(parts) > 2 else ""
    timezone_str = parts[5].strip().lower() if len(parts) > 5 else ""
    if not (platform or language or timezone_str):
        return ""
    seed = f"{platform}|{language}|{timezone_str}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
