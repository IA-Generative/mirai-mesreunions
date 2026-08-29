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

    # Stub du token_store (les jetons ne vont plus dans le cookie : ils sont
    # persistés côté serveur et le cookie ne porte que token_ref).
    token_store = types.ModuleType("app.modules.auth.token_store")
    token_store.saved = []
    token_store.deleted = []

    def _save_tokens(user_sub, *, id_token="", access_token="", refresh_token=None):
        token_store.saved.append({
            "user_sub": user_sub, "id_token": id_token,
            "access_token": access_token, "refresh_token": refresh_token,
        })
        return "ref-du-test"

    token_store.save_tokens = _save_tokens
    token_store.load_tokens = lambda ref=None: (token_store.saved[-1] if token_store.saved else {})
    token_store.update_tokens = lambda ref=None, **k: bool(token_store.saved)
    token_store.delete_tokens = lambda ref=None: token_store.deleted.append(ref)

    modules_pkg = types.ModuleType("app.modules")
    modules_pkg.__path__ = []
    auth_pkg = types.ModuleType("app.modules.auth")
    auth_pkg.__path__ = []
    auth_pkg.token_store = token_store

    monkeypatch.setitem(sys.modules, "app", pkg)
    monkeypatch.setitem(sys.modules, "app.runtime", runtime)
    monkeypatch.setitem(sys.modules, "app.modules", modules_pkg)
    monkeypatch.setitem(sys.modules, "app.modules.auth", auth_pkg)
    monkeypatch.setitem(sys.modules, "app.modules.auth.token_store", token_store)

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
        # Anti-régression cookie : on stocke un booléen compact, PAS la liste
        # des groupes (sinon cookie > ~4 Ko → 502 ingress).
        assert "groups" not in sess["user"]
        assert "is_admin" in sess["user"]


def test_callback_session_stays_compact_with_many_groups(auth_mod, monkeypatch):
    """Un realm renvoyant des dizaines de groupes ne doit pas gonfler la session :
    seul un booléen is_admin est stocké, et il est True si le groupe admin y est."""
    class _TokResp:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": "h.p.s", "access_token": "at"}

    class _ErrResp:
        status_code = 500

        @staticmethod
        def json():
            return {}

    def _req(method, url, **k):
        return _ErrResp() if "userinfo" in url else _TokResp()

    big_groups = [f"/g/groupe-metier-{i}" for i in range(40)] + ["/g/admins"]
    monkeypatch.setattr(auth_mod, "_oidc_request_with_retry", _req)
    monkeypatch.setattr(
        auth_mod, "verify_id_token",
        lambda *a, **k: {"sub": "u", "email": "u@x.test", "name": "U",
                         "preferred_username": "u", "groups": big_groups},
    )
    monkeypatch.setattr(auth_mod, "OIDC_OFFLINE_ACCESS", False, raising=False)

    app = _make_app(auth_mod)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"; sess["oidc_nonce"] = "no"
    resp = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)
    assert resp.status_code in (301, 302)
    with client.session_transaction() as sess:
        assert sess["user"].get("is_admin") is True   # membre de /g/admins
        assert "groups" not in sess["user"]            # pas la liste → cookie compact


def test_callback_keeps_jwt_out_of_cookie(auth_mod, monkeypatch):
    """Incident 2026-08-28 : trois JWT en session → cookie > 4093 octets →
    jeté par le navigateur → boucle /login ↔ Keycloak. Les jetons doivent
    partir dans le token_store et le cookie rester compact même avec des
    JWT réalistes (~1,5 Ko chacun)."""
    big_id = "h." + "p" * 1500 + ".s"
    big_at = "h." + "a" * 1500 + ".s"
    big_rt = "h." + "r" * 1500 + ".s"

    class _TokResp:
        status_code = 200

        @staticmethod
        def json():
            return {"id_token": big_id, "access_token": big_at, "refresh_token": big_rt}

    class _ErrResp:
        status_code = 500

        @staticmethod
        def json():
            return {}

    monkeypatch.setattr(auth_mod, "_oidc_request_with_retry",
                        lambda method, url, **k: _ErrResp() if "userinfo" in url else _TokResp())
    monkeypatch.setattr(
        auth_mod, "verify_id_token",
        lambda *a, **k: {"sub": "user-9", "email": "u@example.test", "name": "U"},
    )
    monkeypatch.setattr(auth_mod, "OIDC_OFFLINE_ACCESS", False, raising=False)

    app = _make_app(auth_mod)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"; sess["oidc_nonce"] = "no"
    resp = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)
    assert resp.status_code in (301, 302)

    # Les JWT sont partis dans le store, pas dans le cookie.
    assert auth_mod.token_store.saved[-1]["access_token"] == big_at
    assert auth_mod.token_store.saved[-1]["refresh_token"] == big_rt
    with client.session_transaction() as sess:
        assert sess.get("token_ref") == "ref-du-test"
        for legacy in ("id_token", "access_token", "refresh_token"):
            assert legacy not in sess

    # Et le Set-Cookie de la réponse tient sous la limite navigateur.
    set_cookie = resp.headers.get("Set-Cookie", "")
    assert 0 < len(set_cookie) < 4093


def test_callback_invalid_grant_loops_at_most_once(auth_mod, monkeypatch):
    """La réponse à invalid_grant est un retour vers /login — mais UNE seule
    fois. Au deuxième invalid_grant consécutif on affiche une erreur au lieu
    d'entretenir une boucle infinie (incident 2026-08-28)."""
    class _BadResp:
        status_code = 400
        text = '{"error":"invalid_grant","error_description":"Code not valid"}'

    monkeypatch.setattr(auth_mod, "_oidc_request_with_retry", lambda *a, **k: _BadResp())

    app = _make_app(auth_mod)
    client = app.test_client()

    with client.session_transaction() as sess:
        sess["oidc_state"] = "st"; sess["oidc_nonce"] = "no"
    resp1 = client.get("/auth/callback?code=abc&state=st", follow_redirects=False)
    assert resp1.status_code in (301, 302)
    assert "/login" in resp1.headers["Location"]

    with client.session_transaction() as sess:
        assert sess.get("oidc_retry") is True
        sess["oidc_state"] = "st2"; sess["oidc_nonce"] = "no2"
    resp2 = client.get("/auth/callback?code=def&state=st2", follow_redirects=False)
    assert resp2.status_code == 400
    with client.session_transaction() as sess:
        assert "oidc_retry" not in sess  # purgé : un futur login repart proprement


def test_logout_uses_store_and_deletes_row(auth_mod):
    app = _make_app(auth_mod)
    client = app.test_client()

    auth_mod.token_store.saved.append({
        "user_sub": "user-9", "id_token": "h.p.s",
        "access_token": "at", "refresh_token": None,
    })
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "user-9"}
        sess["token_ref"] = "ref-du-test"
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "id_token_hint=h.p.s" in resp.headers["Location"]
    assert auth_mod.token_store.deleted  # la rangée serveur est purgée
    with client.session_transaction() as sess:
        assert "user" not in sess and "token_ref" not in sess
