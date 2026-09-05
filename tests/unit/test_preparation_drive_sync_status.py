"""Tests de l'endpoint DTA ``POST /api/v1/preparations/<id>/drive-sync-status``.

Le versement Drive est asynchrone et bavarde : il publie `pending` à la
résolution du dossier, puis `synced` / `failed` / `skipped` à la fin. Ces
battements ne doivent RIEN changer d'autre que l'état de synchro — en
particulier pas `updated_at`, qui pilote le tri « modifiés récemment » de la
liste des préparations.

``main.py`` du device-token-authority est chargé avec ``libs.shared.app.database``
stubbé (pas de Postgres joignable en test) et une session factice ; le modèle
``Preparation`` reste le vrai, pour que ``flag_modified`` porte sur une
instance réellement instrumentée par SQLAlchemy.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Doit passer `is_strong_shared_secret` (≥32 car., aucun marqueur faible
# comme « test- » ou « dev- ») sinon main.py refuse de démarrer.
TOKEN = "kQ9" + "z" * 45


class _FakeQuery:
    """Reproduit le `query(...).filter(...).first()` de l'endpoint."""

    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _FakeSession:
    """Session factice dont le ``commit`` rejoue l'``onupdate`` de SQLAlchemy.

    C'est tout l'enjeu du test : `updated_at` porte un `onupdate`, et l'ORM
    n'écrit la valeur portée par l'attribut que s'il y voit un changement
    NET — une réassignation à l'identique n'en est pas un et laisse
    l'onupdate poser l'heure courante (comportement vérifié sur SQLite).
    Sans cette simulation, un test sur objet nu passerait même si le code
    ne préservait rien.
    """

    def __init__(self, row):
        self._row = row
        self.commits = 0
        self.closed = False

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._row)

    def commit(self):
        from sqlalchemy import inspect

        self.commits += 1
        if self._row is None:
            return
        if not inspect(self._row).attrs.updated_at.history.added:
            self._row.updated_at = datetime.now(timezone.utc)

    def close(self):
        self.closed = True


@pytest.fixture
def dta():
    """Charge main.py avec la couche base stubbée, puis nettoie sys.modules."""
    saved_env = os.environ.get("INTERNAL_API_TOKEN")
    saved_modules = {
        name: sys.modules.get(name)
        for name in ("libs.shared.app.database", "libs.shared.app.config",
                     "dta_main_under_test")
    }
    os.environ["INTERNAL_API_TOKEN"] = TOKEN
    sys.modules.pop("libs.shared.app.config", None)

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.init_tables = MagicMock()
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    spec = importlib.util.spec_from_file_location(
        "dta_main_under_test",
        os.path.join(ROOT, "services", "device-token-authority", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        for name, previous in saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        if saved_env is None:
            os.environ.pop("INTERNAL_API_TOKEN", None)
        else:
            os.environ["INTERNAL_API_TOKEN"] = saved_env


def _preparation(**kwargs):
    from libs.shared.app.models import Preparation
    from sqlalchemy.orm.attributes import instance_state

    defaults = {
        "user_sub": "user-a",
        "subject": "COPIL",
        "updated_at": datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    prep = Preparation(**defaults)
    # Passe l'instance à l'état « chargée depuis la base » : sans ça tous les
    # champs du constructeur comptent comme modifiés et l'onupdate simulé ne
    # se déclencherait jamais.
    instance_state(prep)._commit_all(prep.__dict__)
    return prep


def _post(mod, session, body, *, prep_id="prep-1", token=TOKEN):
    mod.SessionLocal = lambda: session
    client = mod.app.test_client()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        f"/api/v1/preparations/{prep_id}/drive-sync-status",
        json=body, headers=headers,
    )


# ─── Auth / validation ──────────────────────────────────────────────


def test_requires_internal_bearer(dta):
    resp = _post(dta, _FakeSession(_preparation()),
                 {"user_sub": "user-a", "status": "synced"}, token="")
    assert resp.status_code == 401


def test_rejects_unknown_status(dta):
    resp = _post(dta, _FakeSession(_preparation()),
                 {"user_sub": "user-a", "status": "en-cours"})
    assert resp.status_code == 400


def test_accepts_skipped_as_a_first_class_status(dta):
    """« Pas de Drive » n'est pas un échec : sans cet état l'UI proposerait
    un « Réessayer » qui ne pourra jamais aboutir."""
    prep = _preparation()
    resp = _post(dta, _FakeSession(prep), {"user_sub": "user-a", "status": "skipped"})
    assert resp.status_code == 200
    assert resp.get_json()["drive_sync_status"] == "skipped"
    assert prep.drive_sync_status == "skipped"


def test_unknown_preparation_returns_404(dta):
    resp = _post(dta, _FakeSession(None), {"user_sub": "user-a", "status": "synced"})
    assert resp.status_code == 404


# ─── Effets ─────────────────────────────────────────────────────────


def test_preserves_updated_at(dta):
    """Le versement dure ~1 min : sans ça le brief remonterait en tête des
    « modifiés récemment » bien après la dernière action de son auteur."""
    before = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    prep = _preparation(updated_at=before)
    session = _FakeSession(prep)

    resp = _post(dta, session, {"user_sub": "user-a", "status": "synced"})

    assert resp.status_code == 200
    assert session.commits == 1
    assert prep.updated_at == before


def test_synced_stamps_drive_synced_at_and_folder_ids(dta):
    prep = _preparation()
    resp = _post(dta, _FakeSession(prep), {
        "user_sub": "user-a",
        "status": "synced",
        "drive_prep_folder_id": "folder-prep",
        "drive_prep_root_folder_id": "folder-root",
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["drive_prep_folder_id"] == "folder-prep"
    assert body["drive_prep_root_folder_id"] == "folder-root"
    assert prep.drive_synced_at is not None
    assert prep.drive_synced_at > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_folder_ids_absent_from_body_are_left_untouched(dta):
    """Le worker publie `pending` avant de connaître le sous-dossier."""
    prep = _preparation(drive_prep_folder_id="deja-connu",
                        drive_prep_root_folder_id="racine")
    resp = _post(dta, _FakeSession(prep), {"user_sub": "user-a", "status": "failed"})
    assert resp.status_code == 200
    assert prep.drive_prep_folder_id == "deja-connu"
    assert prep.drive_prep_root_folder_id == "racine"
    assert prep.drive_synced_at is None


def test_serialization_exposes_the_root_folder_cache(dta):
    """`drive_prep_root_folder_id` sert de cache — invisible, il ferait
    recréer un dossier « Préparations de réunion » à chaque synchro."""
    prep = _preparation(drive_prep_root_folder_id="racine-42")
    out = dta._preparation_to_dict(prep)
    assert out["drive_prep_root_folder_id"] == "racine-42"
