"""Tests de la politique serveur d'activation du traitement coûteux (PA-01).

Un utilisateur standard ne doit pas pouvoir activer Whisper + LLM à volonté ;
le défaut est OFF et l'activation est pilotée côté serveur.
"""

from libs.shared.app.security import resolve_auto_transcribe


def test_default_policy_is_off_ignores_user_request():
    env = {}  # AUTO_TRANSCRIBE_POLICY absent ⇒ off
    assert resolve_auto_transcribe(True, env=env) is False
    assert resolve_auto_transcribe(False, env=env) is False


def test_off_policy_ignores_even_admin():
    env = {"AUTO_TRANSCRIBE_POLICY": "off"}
    assert resolve_auto_transcribe(True, is_admin=True, env=env) is False


def test_user_policy_honors_request():
    env = {"AUTO_TRANSCRIBE_POLICY": "user"}
    assert resolve_auto_transcribe(True, env=env) is True
    assert resolve_auto_transcribe(False, env=env) is False
    assert resolve_auto_transcribe(None, env=env) is False


def test_admin_policy_requires_admin():
    env = {"AUTO_TRANSCRIBE_POLICY": "admin"}
    assert resolve_auto_transcribe(True, is_admin=True, env=env) is True
    assert resolve_auto_transcribe(True, is_admin=False, env=env) is False


def test_on_policy_always_true():
    env = {"AUTO_TRANSCRIBE_POLICY": "on"}
    assert resolve_auto_transcribe(False, env=env) is True


def test_unknown_policy_falls_back_to_off():
    env = {"AUTO_TRANSCRIBE_POLICY": "yolo"}
    assert resolve_auto_transcribe(True, env=env) is False
