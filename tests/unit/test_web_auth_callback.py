"""Tests du callback OIDC de mesreunions-web (durcissement PA-02).

L'identité doit provenir d'un id_token vérifié cryptographiquement
(signature + nonce). Un échec de vérification refuse la connexion ; il n'y
a plus de repli sur un décodage non vérifié.

Le blueprint dépend de ``app.runtime`` : on le stube avant import.
"""

import importlib.util
import pathlib
import sys
import types

import pytest
from flask import Flask


ROOT = pathlib.Path(__file__).resolve().parents[2]
BP_PATH = ROOT / "services" / "mesreunions-web" / "app" / "modules" / "auth" / "routes.py"


@pytest.fixture
def auth_mod(monkeypatch):
    runtime = types.ModuleType("app.runtime")

    class _Cfg:
        issuer = "https://sso.example.test/realms/openwebui"
        client_id = "mes-reunions"
        client_secret = "s" * 40
        redirect_uri = "https://web.example.test/auth/callback"

    runtime.get_oidc_cfg = lambda: _Cfg()
    runtime.get_oidc_internal_issuer = lambda: "http://keycloak:8080/realms/openwebui"
    runtime.get_oidc_scope = lambda: "openid email profile"
    pkg = types.ModuleType("app")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "app", pkg)
    monkeypatch.setitem(sys.modules, "app.runtime", runtime)

    spec = importlib.util.spec_from_file_location("web_auth_routes_under_test", BP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_app(auth_mod):
    app = Flask(__name__)
    app.secret_key = "k" * 40
    app.register_blueprint(auth_mod.bp)

    @app.route("/")
    def index():
        return "home"

    return app


def test_callback_rejects_unverified_identity(auth_mod, monkeypatch):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "h.p.s", "access_token": "at"}

    monkeypatch.setattr(auth_mod, "_oidc_request_with_retry", lambda *a, **k: _Resp())

    def _boom(*a, **k):
        raise auth_mod.OidcAuthError("bad nonce")

    monkeypatch.setattr(auth_mod, "verify_id_token", _boom)

    app = _make_app(auth_mod)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"
        sess["oidc_nonce"] = "no"
    resp = client.get("/auth/callback?code=abc&state=st")
    assert resp.status_code == 400
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_callback_accepts_verified_identity(auth_mod, monkeypatch):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "h.p.s", "access_token": "at"}

    # userinfo en erreur → enrichissement ignoré, identité = id_token vérifié.
    class _ErrResp:
        status_code = 500

        @staticmethod
        def json():
            return {}

    def _req(method, url, **k):
        return _ErrResp() if "userinfo" in url else _Resp()

    monkeypatch.setattr(auth_mod, "_oidc_request_with_retry", _req)
    monkeypatch.setattr(
        auth_mod, "verify_id_token",
        lambda *a, **k: {"sub": "user-9", "email": "u@example.test", "name": "U"},
    )
    monkeypatch.setattr(auth_mod, "OIDC_OFFLINE_ACCESS", False, raising=False)

    app = _make_app(auth_mod)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"
        sess["oidc_nonce"] = "no"
    resp = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess["user"]["sub"] == "user-9"
