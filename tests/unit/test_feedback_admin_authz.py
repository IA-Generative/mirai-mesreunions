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


def test_is_admin_fail_closed_without_roles(feedback_mod):
    assert feedback_mod._is_admin(None) is False
    assert feedback_mod._is_admin({}) is False
    assert feedback_mod._is_admin({"sub": "u", "roles": []}) is False
    assert feedback_mod._is_admin({"roles": "admin"}) is False  # type invalide


def test_is_admin_true_only_with_admin_role(feedback_mod):
    assert feedback_mod._is_admin({"roles": ["user", "admin"]}) is True
    assert feedback_mod._is_admin({"roles": ["ADMIN"]}) is True
    assert feedback_mod._is_admin({"roles": ["editor"]}) is False


def test_ensure_admin_or_403_blocks_non_admin(feedback_mod):
    import flask

    app = flask.Flask(__name__)
    with app.test_request_context():
        resp = feedback_mod._ensure_admin_or_403({"roles": []})
        assert resp is not None
        body, status = resp
        assert status == 403
