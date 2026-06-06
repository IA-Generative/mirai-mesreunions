"""Tests des décorateurs `require_auth` / `require_admin`.

Les chemins JWT-réels (signature, JWKS, exp) sont laissés à authlib ;
ici on couvre le contrat décorateur (présence/absence header, bypass
DEV, rôle admin).
"""

from unittest.mock import patch

import pytest
from flask import Flask, jsonify

from services.video_ingest.app import auth


@pytest.fixture
def app_with_routes():
    app = Flask(__name__)

    @app.get("/user")
    @auth.require_auth
    def user():
        from flask import g
        return jsonify({"sub": g.user_sub})

    @app.get("/admin")
    @auth.require_admin
    def admin():
        return jsonify({"ok": True})

    return app


def test_no_auth_header_returns_401(app_with_routes, monkeypatch):
    monkeypatch.delenv("VIDEO_INGEST_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("VIDEO_INGEST_OIDC_JWKS_URL", "http://example.invalid/jwks")
    c = app_with_routes.test_client()
    resp = c.get("/user")
    assert resp.status_code == 401


def test_dev_bypass_when_env_set(app_with_routes, monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_AUTH_DISABLED", "1")
    c = app_with_routes.test_client()
    resp = c.get("/user")
    assert resp.status_code == 200
    assert resp.get_json() == {"sub": "dev-anon"}


def test_dev_bypass_honors_x_dev_user_sub(app_with_routes, monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_AUTH_DISABLED", "1")
    c = app_with_routes.test_client()
    resp = c.get("/user", headers={"X-Dev-User-Sub": "tester-42"})
    assert resp.get_json() == {"sub": "tester-42"}


def test_admin_route_requires_role(app_with_routes, monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_AUTH_DISABLED", "1")
    c = app_with_routes.test_client()
    # En bypass DEV, pas de rôle → 403
    resp = c.get("/admin")
    assert resp.status_code == 403


def test_verify_bearer_rejects_empty_token():
    with pytest.raises(auth.AuthError):
        auth.verify_bearer("")


def test_verify_bearer_requires_jwks_url(monkeypatch):
    monkeypatch.delenv("VIDEO_INGEST_OIDC_JWKS_URL", raising=False)
    auth.reset_cache_for_tests()
    with pytest.raises(auth.AuthError) as exc:
        auth.verify_bearer("anything.anything.anything")
    assert exc.value.status == 500


# ─── PA-05 : audience obligatoire (fail-closed) ───────────────────────────

def test_startup_refuses_missing_audience(monkeypatch):
    monkeypatch.delenv("VIDEO_INGEST_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("VIDEO_INGEST_OIDC_AUDIENCE", raising=False)
    with pytest.raises(RuntimeError):
        auth.assert_startup_auth_config()


def test_startup_ok_with_audience(monkeypatch):
    monkeypatch.delenv("VIDEO_INGEST_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("VIDEO_INGEST_OIDC_AUDIENCE", "mes-reunions")
    auth.assert_startup_auth_config()  # ne lève pas


# ─── PA-03 : garde de production sur le bypass d'auth ──────────────────────

def test_startup_refuses_auth_disabled_in_prod(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_AUTH_DISABLED", "1")
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(RuntimeError):
        auth.assert_startup_auth_config()


def test_startup_allows_auth_disabled_in_dev(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_AUTH_DISABLED", "1")
    monkeypatch.setenv("ENVIRONMENT", "development")
    auth.assert_startup_auth_config()  # bypass dev autorisé
