import os
import sys
import importlib.util

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULE_PATH = os.path.join(ROOT, "libs", "shared", "app", "device_token.py")
SPEC = importlib.util.spec_from_file_location("device_token", MODULE_PATH)
DEVICE_TOKEN = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(DEVICE_TOKEN)

create_device_token = DEVICE_TOKEN.create_device_token
verify_device_token = DEVICE_TOKEN.verify_device_token
utc_now_ts = DEVICE_TOKEN.utc_now_ts


def test_create_and_verify_device_token_roundtrip():
    secret = "super-secret-for-tests"
    payload = {
        "device_id": "abc-123",
        "user_sub": "user-1",
        "qr_token": "qr-1",
        "retention_until": utc_now_ts() + 3600,
        "iat": utc_now_ts(),
    }
    token = create_device_token(payload, secret)
    decoded = verify_device_token(token, secret)
    assert decoded == payload


def test_verify_device_token_rejects_tampered_payload():
    secret = "super-secret-for-tests"
    payload = {"device_id": "abc-123", "retention_until": utc_now_ts() + 3600}
    token = create_device_token(payload, secret)
    left, right = token.split(".", 1)
    tampered = f"{left}X.{right}"
    try:
        verify_device_token(tampered, secret)
        assert False, "expected ValueError"
    except ValueError:
        assert True


def test_verify_device_token_rejects_wrong_secret():
    payload = {"device_id": "abc-123", "retention_until": utc_now_ts() + 3600}
    token = create_device_token(payload, "secret-a")
    try:
        verify_device_token(token, "secret-b")
        assert False, "expected ValueError"
    except ValueError:
        assert True
