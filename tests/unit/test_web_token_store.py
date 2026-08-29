"""Tests du token_store de mesreunions-web (table ``web_session_tokens``).

Contexte : incident 2026-08-28 — les trois JWT (id/access/refresh) stockés
dans le cookie de session dépassaient ~4093 octets, les navigateurs
jetaient le cookie et l'utilisateur bouclait /login ↔ Keycloak. Les jetons
vivent désormais côté serveur ; ces tests vérifient le round-trip, le
chiffrement Fernet optionnel, la purge TTL et la suppression au logout.

DB réelle : SQLite fichier temporaire (les modèles ExternalBase sont
compatibles via ``with_variant``). ``app.runtime`` est stubé avant import,
comme dans test_web_auth_callback.py.
"""

import importlib.util
import os
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STORE_PATH = (ROOT / "services" / "mesreunions-web" / "app" / "modules"
              / "auth" / "token_store.py")


def _load_by_path(name: str, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Tout est chargé par chemin : d'autres fichiers de tests laissent des
    # stubs de libs.shared.app.* dans sys.modules et rendraient le registre
    # SQLAlchemy incohérent selon l'ordre d'exécution de la suite.
    models = _load_by_path("web_token_store_models", ROOT / "libs" / "shared" / "app" / "models.py")
    engine = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    models.ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    runtime = types.ModuleType("app.runtime")
    runtime.session_scope = lambda: factory()
    pkg = types.ModuleType("app")
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, "app", pkg)
    monkeypatch.setitem(sys.modules, "app.runtime", runtime)
    monkeypatch.delenv("OIDC_REFRESH_TOKEN_FERNET_KEY", raising=False)

    mod = _load_by_path("web_token_store_under_test", STORE_PATH)
    monkeypatch.setattr(mod, "secrets_crypto", _load_by_path(
        "web_token_store_crypto", ROOT / "libs" / "shared" / "app" / "secrets_crypto.py"))
    monkeypatch.setattr(mod, "WebSessionToken", models.WebSessionToken)

    mod._factory = factory   # pour les assertions directes sur les rangées
    mod._models = models
    return mod


@pytest.fixture
def req_ctx():
    app = Flask(__name__)
    app.secret_key = "k" * 40
    with app.test_request_context():
        yield app


def test_round_trip_plaintext_when_no_fernet_key(store, req_ctx):
    ref = store.save_tokens("user-1", id_token="idt", access_token="at",
                            refresh_token="rt")
    assert ref
    out = store.load_tokens(ref)
    assert out["id_token"] == "idt"
    assert out["access_token"] == "at"
    assert out["refresh_token"] == "rt"
    with store._factory() as db:
        row = db.query(store._models.WebSessionToken).filter_by(token_ref=ref).one()
        assert row.encrypted is False
        assert row.access_token == "at"  # clair assumé (clé absente)


def test_round_trip_encrypted_with_fernet_key(store, req_ctx, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("OIDC_REFRESH_TOKEN_FERNET_KEY", Fernet.generate_key().decode())

    ref = store.save_tokens("user-2", id_token="idt", access_token="at",
                            refresh_token="rt")
    out = store.load_tokens(ref)
    assert out["access_token"] == "at" and out["refresh_token"] == "rt"
    with store._factory() as db:
        row = db.query(store._models.WebSessionToken).filter_by(token_ref=ref).one()
        assert row.encrypted is True
        assert row.access_token != "at"  # ciphertext Fernet, pas le clair


def test_update_rotates_access_and_refresh(store, req_ctx):
    ref = store.save_tokens("user-3", access_token="at1", refresh_token="rt1")
    assert store.update_tokens(ref, access_token="at2", refresh_token="rt2") is True
    out = store.load_tokens(ref)
    assert out["access_token"] == "at2" and out["refresh_token"] == "rt2"
    # Rotation partielle (Keycloak ne renvoie pas toujours un refresh) :
    assert store.update_tokens(ref, access_token="at3") is True
    out = store.load_tokens(ref)
    assert out["access_token"] == "at3" and out["refresh_token"] == "rt2"


def test_delete_removes_row(store, req_ctx):
    ref = store.save_tokens("user-4", access_token="at")
    store.delete_tokens(ref)
    assert store.load_tokens(ref) == {}
    with store._factory() as db:
        assert db.query(store._models.WebSessionToken).filter_by(token_ref=ref).count() == 0


def test_save_purges_rows_older_than_ttl(store, req_ctx):
    old_ref = store.save_tokens("user-5", access_token="vieux")
    with store._factory() as db:
        db.query(store._models.WebSessionToken).filter_by(token_ref=old_ref).update(
            {"updated_at": datetime.now(timezone.utc) - timedelta(days=30)})
        db.commit()
    store.save_tokens("user-5", access_token="neuf")
    with store._factory() as db:
        assert db.query(store._models.WebSessionToken).filter_by(token_ref=old_ref).count() == 0


def test_load_uses_session_ref_and_missing_ref_degrades(store, req_ctx):
    from flask import session
    assert store.load_tokens() == {}  # pas de ref → dict vide, pas d'exception
    ref = store.save_tokens("user-6", access_token="at")
    session["token_ref"] = ref
    assert store.load_tokens()["access_token"] == "at"
