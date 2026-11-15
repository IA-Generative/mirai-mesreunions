"""QR Enrollment Portal : Passage2 → compte Keycloak → émission du QR.

Le service a un dossier avec tiret : on charge ``app/portal.py`` par chemin.
La base externe est une SQLite en mémoire limitée aux tables utilisées.
"""

import base64
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from libs.shared.app.keycloak_users import AmbiguousUserError, KeycloakLookupError
from libs.shared.app.models import (
    ExternalBase, UploadedFile, UploadSession, UploadTokenOption,
)
from libs.shared.app.passage2 import Passage2Config
from libs.shared.app.rate_limit import SlidingWindowLimiter

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORTAL = ROOT / "services" / "qr-enrollment-portal" / "app" / "portal.py"
SECRET = "s3cr3t-proxy-" + "x" * 32
EMAIL = "jeanne.martin@interieur.gouv.fr"
USER = {"sub": "kc-sub-1", "email": EMAIL, "name": "Jeanne Martin"}
CSRF = {"X-Requested-With": "qr-enrollment-portal"}


def _load_portal():
    spec = importlib.util.spec_from_file_location("qr_portal_under_test", PORTAL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


portal = _load_portal()


class Env:
    def __init__(self):
        engine = create_engine("sqlite://")
        ExternalBase.metadata.create_all(engine, tables=[
            UploadSession.__table__, UploadedFile.__table__, UploadTokenOption.__table__,
        ])
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.accounts = {EMAIL: USER}
        self.issued = []
        self.internal_calls = []
        self.devices = []
        self.issue_error = None
        self.resolve_error = None

    def resolve(self, email):
        if self.resolve_error:
            raise self.resolve_error
        return self.accounts.get(email)

    def issue(self, user, ttl_minutes, max_uploads, auto_transcribe=False):
        if self.issue_error:
            raise self.issue_error
        n = len(self.issued) + 1
        self.issued.append((user, ttl_minutes, max_uploads))
        return {"simple_code": f"ABC{n:03d}", "qr_token": f"qrtoken{n:04d}abcdef",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()}

    def internal(self, method, path, json_body=None, params=None):
        self.internal_calls.append((method, path, json_body, params))
        if path == "/api/v1/devices":
            return self.devices
        if path.endswith("revoke-all"):
            return {"revoked": 2}
        return {}


@pytest.fixture
def env():
    return Env()


@pytest.fixture
def client(env):
    app = portal.create_app(
        passage2=Passage2Config.from_env({"ENVIRONMENT": "production",
                                          "PASSAGE2_PROXY_SECRET": SECRET}),
        resolve_user=env.resolve,
        session_factory=env.sessions,
        upload_base_url="https://depot.example.fr/",
        internal_api=env.internal,
        issue_token=env.issue,
        generate_limiter=SlidingWindowLimiter(max_events=3, window_seconds=60),
    )
    c = app.test_client()
    c.environ_base.update({
        "HTTP_MELLON_NAMEID": EMAIL,
        "HTTP_X_PASSAGE2_PROXY_SECRET": SECRET,
    })
    return c


def test_healthz_needs_no_identity(env):
    app = portal.create_app(
        passage2=Passage2Config.from_env({"PASSAGE2_PROXY_SECRET": SECRET}),
        resolve_user=env.resolve, session_factory=env.sessions,
        upload_base_url="https://d", internal_api=env.internal,
    )
    assert app.test_client().get("/healthz").status_code == 200


def test_page_shows_the_resolved_user(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Jeanne Martin" in resp.get_data(as_text=True)
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_generates_a_qr_bound_to_the_keycloak_account(client, env):
    resp = client.post("/api/generate-code", json={"ttl_minutes": 15}, headers=CSRF)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    assert data["upload_url"] == "https://depot.example.fr/upload/qrtoken0001abcdef"
    assert data["simple_code"] == "ABC001"
    png = base64.b64decode(data["qr_image"].split(",", 1)[1])
    assert png.startswith(b"\x89PNG")

    user, ttl, _ = env.issued[0]
    assert user["sub"] == "kc-sub-1" and ttl == 15
    db = env.sessions()
    row = db.query(UploadSession).one()
    assert (row.user_sub, row.qr_token, row.user_email) == ("kc-sub-1", data["qr_token"], EMAIL)
    assert db.query(UploadTokenOption).one().qr_token == data["qr_token"]


def test_ttl_and_uploads_are_capped(client, env, monkeypatch):
    monkeypatch.setattr(portal, "CODE_TTL_MAX_MINUTES", 60)
    monkeypatch.setattr(portal, "MAX_UPLOADS_PER_SESSION", 10)
    client.post("/api/generate-code", json={"ttl_minutes": 99999, "max_uploads": 500}, headers=CSRF)
    _, ttl, max_uploads = env.issued[0]
    assert (ttl, max_uploads) == (60, 10)
    resp = client.post("/api/generate-code", json={"ttl_minutes": "abc"}, headers=CSRF)
    assert resp.status_code == 400


def test_mutations_require_the_csrf_header(client, env):
    assert client.post("/api/generate-code", json={}).status_code == 403
    assert client.post("/api/my-devices/revoke-all").status_code == 403
    assert env.issued == [] and env.internal_calls == []


def test_rejects_requests_that_bypass_passage2(client):
    client.environ_base["HTTP_X_PASSAGE2_PROXY_SECRET"] = "forged"
    resp = client.post("/api/generate-code", json={}, headers=CSRF)
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "untrusted_proxy"
    assert client.get("/").status_code == 403


@pytest.mark.parametrize("setup, status, code", [
    (lambda e: e.accounts.clear(), 403, "no_account"),
    (lambda e: setattr(e, "resolve_error", AmbiguousUserError("x")), 409, "ambiguous_account"),
    (lambda e: setattr(e, "resolve_error", KeycloakLookupError("x")), 503, "directory_unavailable"),
])
def test_account_resolution_failures(client, env, setup, status, code):
    setup(env)
    resp = client.post("/api/generate-code", json={}, headers=CSRF)
    assert (resp.status_code, resp.get_json()["error"]) == (status, code)
    assert env.issued == []


def test_issuer_unavailable(client, env):
    env.issue_error = ConnectionError("down")
    resp = client.post("/api/generate-code", json={}, headers=CSRF)
    assert resp.status_code == 503
    assert env.sessions().query(UploadSession).count() == 0


def test_generation_is_rate_limited_per_user(client):
    for _ in range(3):
        assert client.post("/api/generate-code", json={}, headers=CSRF).status_code == 200
    assert client.post("/api/generate-code", json={}, headers=CSRF).status_code == 429


def test_devices_are_listed_without_secrets(client, env):
    client.post("/api/generate-code", json={}, headers=CSRF)
    env.devices = [{"device_id": "d1", "device_name": "iPhone", "status": "active",
                    "qr_token": "qrtoken0001abcdef", "device_key": "SECRET-KEY",
                    "fp_hash": "abc", "user_sub": "kc-sub-1"}]
    resp = client.get("/api/my-devices")
    assert resp.status_code == 200
    (device,) = resp.get_json()
    assert device["device_name"] == "iPhone"
    assert device["session_simple_code"] == "ABC001"
    assert "device_key" not in device and "fp_hash" not in device and "qr_token" not in device
    assert env.internal_calls[-1] == ("GET", "/api/v1/devices", None, {"user_sub": "kc-sub-1"})


def test_device_actions_are_scoped_to_the_user(client, env):
    assert client.post("/api/my-devices/d1/rename", json={"device_name": " Pixel "},
                       headers=CSRF).get_json() == {"ok": True}
    assert client.post("/api/my-devices/d1/revoke", headers=CSRF).status_code == 200
    assert client.post("/api/my-devices/revoke-all", headers=CSRF).get_json() == {
        "ok": True, "revoked": 2}
    bodies = [call[2] for call in env.internal_calls]
    assert all(body["user_sub"] == "kc-sub-1" for body in bodies)
    assert bodies[0]["device_name"] == "Pixel"
    assert client.post("/api/my-devices/d1/rename", json={}, headers=CSRF).status_code == 400
