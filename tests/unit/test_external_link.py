"""Tests du lien externe ``/preparer`` et du retour après connexion.

Ce lien est destiné à être posé par des applications tierces : ses
paramètres ne sont pas de confiance, et la destination doit survivre à
l'authentification sans ouvrir de redirection arbitraire.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_INTERNAL_TOKEN = "not-a-real-credential-synthetic-fixture-value"


def _load_web():
    """Charge ``main.py`` avec les effets de bord du boot neutralisés."""
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-long-xxxxx")
    os.environ.setdefault("OIDC_ISSUER", "https://kc.test/realms/test")
    os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
    os.environ.setdefault("OIDC_CLIENT_SECRET", "test-secret")
    os.environ.setdefault("OIDC_REDIRECT_URI", "http://test/auth/callback")

    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name in {"libs.shared", "libs"}:
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)
    for name in ("requests", "authlib", "authlib.integrations",
                 "authlib.integrations.flask_client"):
        stub = sys.modules.get(name)
        if stub is not None and not getattr(stub, "__file__", None):
            sys.modules.pop(name, None)

    web_dir = os.path.join(ROOT, "services", "mesreunions-web")
    if web_dir not in sys.path:
        sys.path.insert(0, web_dir)
    sys.modules.pop("app", None)

    if "pika" not in sys.modules:
        pika = types.ModuleType("pika")
        pika.BlockingConnection = MagicMock()
        pika.ConnectionParameters = MagicMock()
        pika.PlainCredentials = MagicMock()
        pika.exceptions = types.SimpleNamespace(
            AMQPConnectionError=Exception, ChannelClosedByBroker=Exception)
        sys.modules["pika"] = pika
    if "qrcode" not in sys.modules:
        qr = types.ModuleType("qrcode")
        qr.QRCode = MagicMock()
        consts = types.SimpleNamespace(ERROR_CORRECT_M=0)
        qr.constants = consts
        sys.modules["qrcode"] = qr
        sys.modules["qrcode.constants"] = consts

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_k: MagicMock()
    db_stub.init_tables = MagicMock()
    db_stub.with_db_retry = lambda fn, **_k: fn()
    db_stub.__file__ = "<stub>"
    sys.modules["libs.shared.app.database"] = db_stub

    sys.modules.pop("mesreunions_web_external_link_test", None)
    spec = importlib.util.spec_from_file_location(
        "mesreunions_web_external_link_test",
        os.path.join(web_dir, "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def web():
    mod = _load_web()
    mod.app.config["TESTING"] = True
    return mod.app.test_client(), mod


def _login(client):
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "u-1", "email": "u@test"}


# ─── /preparer ────────────────────────────────────────────────────


def test_preparer_without_params_opens_an_empty_wizard(web):
    client, _mod = web
    _login(client)
    r = client.get("/preparer")
    assert r.status_code == 302
    assert "tab=brief" in r.headers["Location"]
    assert "action=new" in r.headers["Location"]


def test_preparer_forwards_known_params(web):
    client, _mod = web
    _login(client)
    r = client.get("/preparer?sujet=Comit%C3%A9+budget&duree=60&type=steering_committee&ref=ext-42")
    loc = r.headers["Location"]
    assert "subject=Comit" in loc
    assert "duration=60" in loc
    assert "meeting_type=steering_committee" in loc
    assert "external_ref=ext-42" in loc


def test_preparer_ignores_unknown_params(web):
    client, _mod = web
    _login(client)
    r = client.get("/preparer?inconnu=valeur&sujet=Test")
    loc = r.headers["Location"]
    assert "inconnu" not in loc
    assert "subject=Test" in loc


def test_preparer_caps_and_sanitizes_hostile_values(web):
    """Les paramètres viennent d'un tiers et finissent dans le prompt."""
    client, _mod = web
    _login(client)
    r = client.get("/preparer?sujet=" + ("A" * 900))
    loc = r.headers["Location"]
    # Borné court : pas de sujet de 900 caractères qui parte au modèle.
    assert loc.count("A") <= 300


def test_preparer_requires_auth_and_keeps_the_destination(web):
    """Sans session, on part se connecter en conservant le contexte."""
    client, _mod = web
    r = client.get("/preparer?sujet=Test&ref=ext-9")
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert "/login" in loc
    assert "next=" in loc
    assert "preparer" in loc


# ─── Retour après connexion ───────────────────────────────────────


def test_safe_next_accepts_internal_paths(web):
    _client, mod = web
    from app.modules.auth.routes import safe_next_target
    assert safe_next_target("/preparer?sujet=x") == "/preparer?sujet=x"
    assert safe_next_target("/") == "/"


@pytest.mark.parametrize("hostile", [
    "https://evil.tld/steal",
    "//evil.tld/steal",
    "http://evil.tld",
    "/ok\\..\\evil",
    "/ok\nSet-Cookie: x=1",
    "javascript:alert(1)",
    "",
    None,
])
def test_safe_next_refuses_anything_but_an_internal_path(web, hostile):
    """Un lien entrant ne doit pas pouvoir faire rebondir vers un tiers."""
    _client, mod = web
    from app.modules.auth.routes import safe_next_target
    assert safe_next_target(hostile) is None
