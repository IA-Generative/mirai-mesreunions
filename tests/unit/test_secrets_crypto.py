"""
Unit tests for libs.shared.app.secrets_crypto (Fernet wrapper).

The Fernet key is read from the env var ``OIDC_REFRESH_TOKEN_FERNET_KEY``
at every call (no module-level caching) so the rotation procedure can swap
it via a rolling restart without surprise. These tests exercise that
behaviour explicitly.
"""

import importlib.util
import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# cryptography is a hard dep here. Skip cleanly if not installed locally
# (CI image has it).
pytest.importorskip("cryptography")

MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "secrets_crypto.py")
SPEC = importlib.util.spec_from_file_location("secrets_crypto_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)


from cryptography.fernet import Fernet


@pytest.fixture(autouse=True)
def _reset_env():
    """Clear the env var between tests to avoid leakage."""
    original = os.environ.pop("OIDC_REFRESH_TOKEN_FERNET_KEY", None)
    yield
    if original is None:
        os.environ.pop("OIDC_REFRESH_TOKEN_FERNET_KEY", None)
    else:
        os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = original


# --- is_configured / boot guard --------------------------------------------

def test_is_configured_false_when_env_missing():
    assert MOD.is_configured() is False


def test_is_configured_false_when_env_blank():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = "   "
    assert MOD.is_configured() is False


def test_is_configured_false_when_key_invalid():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = "not-a-fernet-key"
    assert MOD.is_configured() is False


def test_is_configured_true_when_valid_key():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    assert MOD.is_configured() is True


# --- encrypt / decrypt happy paths ----------------------------------------

def test_roundtrip_short_string():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    plaintext = "hello"
    ct = MOD.encrypt(plaintext)
    assert ct != plaintext  # actually encrypted
    assert MOD.decrypt(ct) == plaintext


def test_roundtrip_realistic_jwt_like_string():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    # Real refresh tokens look like long opaque strings.
    plaintext = "eyJhbGciOiJSUzI1NiIs..." + "x" * 800
    ct = MOD.encrypt(plaintext)
    assert MOD.decrypt(ct) == plaintext


def test_ciphertext_is_url_safe_ascii():
    """The ciphertext must be safe to put in JSON / DB text columns."""
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    ct = MOD.encrypt("payload")
    ct.encode("ascii")  # would raise if not ASCII


# --- error guards ---------------------------------------------------------

def test_encrypt_fails_when_env_missing():
    with pytest.raises(RuntimeError):
        MOD.encrypt("anything")


def test_decrypt_fails_when_env_missing():
    with pytest.raises(RuntimeError):
        MOD.decrypt("anything")


def test_encrypt_fails_when_key_garbage():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = "not-a-key-at-all"
    with pytest.raises(RuntimeError):
        MOD.encrypt("payload")


def test_encrypt_rejects_none():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    with pytest.raises(ValueError):
        MOD.encrypt(None)


def test_decrypt_rejects_empty():
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    with pytest.raises(ValueError):
        MOD.decrypt("")


def test_decrypt_rejects_tampered_ciphertext():
    """A flipped byte must not silently produce wrong plaintext."""
    from cryptography.fernet import InvalidToken
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = Fernet.generate_key().decode()
    ct = MOD.encrypt("payload")
    tampered = ct[:-2] + ("XX" if ct[-2:] != "XX" else "YY")
    with pytest.raises(InvalidToken):
        MOD.decrypt(tampered)


def test_decrypt_rejects_wrong_key():
    """Encrypting with one key then decrypting with another must fail loudly."""
    from cryptography.fernet import InvalidToken
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = key1
    ct = MOD.encrypt("payload")
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = key2
    with pytest.raises(InvalidToken):
        MOD.decrypt(ct)


# --- rotation behaviour --------------------------------------------------

def test_key_rotation_via_env_var_swap():
    """If the env var is updated mid-process (rolling restart), the next
    encrypt call must use the new key — no module-level caching."""
    key1 = Fernet.generate_key().decode()
    key2 = Fernet.generate_key().decode()
    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = key1
    ct1 = MOD.encrypt("data")

    os.environ["OIDC_REFRESH_TOKEN_FERNET_KEY"] = key2
    ct2 = MOD.encrypt("data")

    # Same plaintext, different keys → different ciphertexts.
    assert ct1 != ct2
    # Old ciphertext can no longer be read with the new key.
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        MOD.decrypt(ct1)
