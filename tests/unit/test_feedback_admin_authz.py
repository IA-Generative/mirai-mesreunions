"""Tests d'autorisation fonction (BFLA) du blueprint feedback.

Vérifie que le contrôle de rôle admin est fail-closed : un utilisateur sans
rôle ``admin`` ne peut pas atteindre les routes d'administration.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BP_PATH = ROOT / "services" / "mesreunions-web" / "app" / "modules" / "feedback" / "routes.py"


@pytest.fixture
def feedback_mod(monkeypatch):
    # Stubs des dépendances internes du blueprint.
    app_pkg = types.ModuleType("app"); app_pkg.__path__ = []
    shared = types.ModuleType("app.shared")
    shared.get_current_user = lambda: None
    shared.require_auth = lambda f: f
    runtime = types.ModuleType("app.runtime")
    runtime.session_scope = lambda: None
    modules_pkg = types.ModuleType("app.modules"); modules_pkg.__path__ = []
    sessions_pkg = types.ModuleType("app.modules.sessions"); sessions_pkg.__path__ = []
    sessions_service = types.ModuleType("app.modules.sessions.service")
    for name, mod in [
        ("app", app_pkg), ("app.shared", shared), ("app.runtime", runtime),
        ("app.modules", modules_pkg), ("app.modules.sessions", sessions_pkg),
        ("app.modules.sessions.service", sessions_service),
    ]:
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("feedback_routes_under_test", BP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_is_admin_fail_closed_without_group(feedback_mod, monkeypatch):
    monkeypatch.delenv("ADMIN_ALLOWED_USERS", raising=False)
    assert feedback_mod._is_admin(None) is False
    assert feedback_mod._is_admin({}) is False
    assert feedback_mod._is_admin({"sub": "u", "groups": []}) is False
    assert feedback_mod._is_admin({"groups": ["/g/users"]}) is False


def test_is_admin_honors_session_boolean(feedback_mod, monkeypatch):
    """La session stocke un booléen compact is_admin (pas la liste groups)."""
    monkeypatch.delenv("ADMIN_ALLOWED_USERS", raising=False)
    assert feedback_mod._is_admin({"sub": "u", "is_admin": True}) is True
    assert feedback_mod._is_admin({"sub": "u", "is_admin": False}) is False
    assert feedback_mod._is_admin({"sub": "u"}) is False  # ni bool ni groups


def test_is_admin_true_with_admin_group(feedback_mod, monkeypatch):
    monkeypatch.delenv("ADMIN_ALLOWED_USERS", raising=False)
    assert feedback_mod._is_admin({"groups": ["/g/users", "/g/admins"]}) is True
    # Tolérant casse/slash.
    assert feedback_mod._is_admin({"groups": ["g/Admins"]}) is True


def test_ensure_admin_or_403_blocks_non_member(feedback_mod, monkeypatch):
    import flask

    monkeypatch.delenv("ADMIN_ALLOWED_USERS", raising=False)
    app = flask.Flask(__name__)
    with app.test_request_context():
        resp = feedback_mod._ensure_admin_or_403({"groups": ["/g/users"]})
        assert resp is not None
        body, status = resp
        assert status == 403
