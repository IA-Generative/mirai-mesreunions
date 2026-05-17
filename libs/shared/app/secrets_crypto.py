"""
Symmetric Fernet encryption for at-rest secrets persisted in the database.

Today's only consumer is the OIDC refresh token cache (``oidc_refresh_tokens``
table populated at user login by mesreunions-web/admin-console and read by
internal-ingester at MCR push time). Other future at-rest secrets — e.g. webhook
signing keys, third-party service tokens — should reuse this helper rather
than reinvent a Fernet wrapper.

Key management
--------------
The Fernet key is read once from the env var ``OIDC_REFRESH_TOKEN_FERNET_KEY``
(URL-safe base64, 32 bytes once decoded — exactly what
``Fernet.generate_key()`` produces). The same key value MUST be deployed in
both K8s namespaces that use it (audio-internal for internal-ingester and code-
generator/admin-console in prod-bêta where these services live), otherwise
ciphertexts written by one service can't be decrypted by another.

Rotation: re-encrypt the entire ``oidc_refresh_tokens`` table with the new
key, then swap the env var. A scripted procedure with dual-key transition
is out of scope here (see plan).

Operational guardrails
----------------------
- ``encrypt`` / ``decrypt`` raise ``RuntimeError`` if the env var is missing
  or invalid, fail-fast at first call rather than silently producing garbage
  ciphertext that future deploys couldn't decode.
- We deliberately do NOT cache a singleton ``Fernet`` instance at import
  time — that would freeze any rotation that swaps the env var via a rolling
  restart. The cost of building a Fernet object per call is negligible
  (~microseconds) compared to the surrounding DB / network cost.
"""

from __future__ import annotations

import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


_ENV_KEY = "OIDC_REFRESH_TOKEN_FERNET_KEY"


def _load_key() -> bytes:
    raw = os.getenv(_ENV_KEY, "").strip()
    if not raw:
        raise RuntimeError(
            f"{_ENV_KEY} is not set. Provision the K8s Secret "
            f"oidc-refresh-token-encryption (key: 'key') and ensure the "
            f"deployment mounts it as env."
        )
    try:
        # Fernet itself validates the key length when constructing.
        Fernet(raw.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{_ENV_KEY} is not a valid Fernet key. Generate a new one with: "
            f"python3 -c 'from cryptography.fernet import Fernet; "
            f"print(Fernet.generate_key().decode())'"
        ) from exc
    return raw.encode("utf-8")


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string and return a URL-safe base64 ciphertext."""
    if plaintext is None:
        raise ValueError("Cannot encrypt None")
    return Fernet(_load_key()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext back to its original UTF-8 string."""
    if not ciphertext:
        raise ValueError("Cannot decrypt empty ciphertext")
    try:
        return Fernet(_load_key()).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        # Could be: tampered ciphertext, wrong key, or expired Fernet token (default
        # never expires, but be defensive). Caller must treat as non-recoverable.
        raise InvalidToken("Ciphertext decryption failed — wrong key, tampered data or unknown format") from exc


def is_configured() -> bool:
    """Return True iff the Fernet key is available — useful for boot-time checks."""
    raw: Optional[str] = os.getenv(_ENV_KEY, "").strip()
    if not raw:
        return False
    try:
        Fernet(raw.encode("utf-8"))
        return True
    except (ValueError, TypeError):
        return False
