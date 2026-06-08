"""Tests de durcissement d'authentification de la console d'administration.

Couvre :
  - autorisation admin fail-closed : une liste d'accès vide refuse le
    démarrage (jamais d'ouverture à tout le realm) ;
  - callback OIDC : l'identité provient d'un id_token vérifié
    cryptographiquement (signature + nonce) ; un échec de vérification
    refuse la connexion ; plus aucun repli sur un décodage non vérifié.

Le service ``admin-console`` a un nom de dossier avec tiret : on le charge
par chemin via importlib (un module frais par scénario pour réévaluer la
garde de démarrage avec l'environnement voulu).
"""

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = ROOT / "services" / "admin-console" / "app" / "main.py"


def _load(monkeypatch, env, modname):
    for key, val in env.items():
        if val is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, val)
    spec = importlib.util.spec_from_file_location(modname, MAIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE_ENV = {
    "ENVIRONMENT": "development",
    "SECRET_KEY": "x" * 40,
    "OIDC_ISSUER": "https://sso.example.test/realms/openwebui",
    "OIDC_CLIENT_ID": "mes-reunions",
    "OIDC_CLIENT_SECRET": "y" * 40,
    "OIDC_REDIRECT_URI": "https://admin.example.test/auth/callback",
    "OIDC_INTERNAL_ISSUER": "http://keycloak:8080/realms/openwebui",
}


def test_no_admin_mechanism_refuses_boot(monkeypatch):
    """Ni groupe ni liste d'accès ⇒ refus de démarrage (fail-closed)."""
    from libs.shared.app.oidc_auth import AuthStartupError

    env = {**BASE_ENV, "ADMIN_ALLOWED_USERS": "", "ADMIN_GROUP": ""}
    with pytest.raises(AuthStartupError):
        _load(monkeypatch, env, "admin_main_no_mechanism")


def test_empty_allowlist_with_group_boots(monkeypatch):
    """Liste vide mais admin par groupe (défaut /g/admins) ⇒ boot OK."""
    env = {**BASE_ENV, "ADMIN_ALLOWED_USERS": ""}
    mod = _load(monkeypatch, env, "admin_main_group_boot")
    assert mod.application is not None


def test_default_allowlist_boots(monkeypatch):
    env = {**BASE_ENV, "ADMIN_ALLOWED_USERS": "admin"}
    mod = _load(monkeypatch, env, "admin_main_ok_boot")
    assert mod.application is not None


def test_callback_rejects_unverified_identity(monkeypatch):
    """Si l'id_token ne se vérifie pas (signature/nonce), connexion refusée."""
    from libs.shared.app.oidc_auth import OidcAuthError

    env = {**BASE_ENV, "ADMIN_ALLOWED_USERS": "admin"}
    mod = _load(monkeypatch, env, "admin_main_reject")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "header.payload.sig", "access_token": "at"}

    monkeypatch.setattr(mod.req, "post", lambda *a, **k: _Resp())

    def _boom(*a, **k):
        raise OidcAuthError("bad nonce")

    monkeypatch.setattr(mod, "verify_id_token", _boom)

    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"
        sess["oidc_nonce"] = "no"
    resp = client.get("/auth/callback?code=abc&state=st")
    assert resp.status_code == 400
    # L'identité ne doit pas avoir été posée en session.
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_callback_accepts_verified_identity(monkeypatch):
    """id_token vérifié ⇒ identité posée ; userinfo en enrichissement only."""
    env = {**BASE_ENV, "ADMIN_ALLOWED_USERS": "admin"}
    mod = _load(monkeypatch, env, "admin_main_accept")

    class _PostResp:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "h.p.s", "access_token": "at"}

    class _UserinfoResp:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(mod.req, "post", lambda *a, **k: _PostResp())
    monkeypatch.setattr(mod.req, "get", lambda *a, **k: _UserinfoResp())
    monkeypatch.setattr(
        mod, "verify_id_token",
        lambda *a, **k: {"sub": "admin", "email": "admin@example.test",
                         "preferred_username": "admin"},
    )

    client = mod.application.test_client()
    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"
        sess["oidc_nonce"] = "no"
    resp = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess["user"]["sub"] == "admin"
