"""
Unit tests for the meeting-brief CRUD endpoints exposed by token-issuer.

The 7 endpoints listed in piste 1 du doc/meeting-prep-journey.md sont en
réalité hébergés en zone interne (token-issuer) — code-generator (zone
externe) ne fait que relayer. On exerce donc directement les endpoints
``/api/v1/briefs/*`` via Flask test client, avec un SessionLocal câblé
sur SQLite in-memory.

Cas couverts :
  - POST persiste et renvoie brief_id
  - GET liste filtre trashed_at IS NULL
  - GET liste isole par user_sub (user A ne voit pas brief de user B)
  - DELETE bascule en corbeille
  - restore le ramène
  - rename change le title
  - amend modifie brief_json sans toucher autres champs
  - 404 pour brief inexistant ou pour autre user_sub
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_INTERNAL_TOKEN = "***REMOVED-FIXTURE-TOKEN***"  # 35 chars, no banned prefix


def _load_token_issuer():
    """Load token-issuer's main.py with init_tables / create_session_factory
    stubbed so we don't need a real Postgres at module-import time.

    Returns the loaded module. The caller wires ``mod.SessionLocal`` to a
    SQLite-backed factory before exercising any route.
    """
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    # Purge stubs left by sibling tests (test_puller_perform_pull / test_oidc_refresh_store)
    # that replace libs.shared.app.* with plain ModuleTypes — they break token-issuer's
    # `from libs.shared.app.config import …`.
    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name == "libs.shared" or name == "libs":
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)
    # Pika isn't installed in the test env — stub it before libs.shared.app
    # imports queue_helper transitively via __init__.
    if "pika" not in sys.modules:
        pika_stub = types.ModuleType("pika")
        pika_stub.BlockingConnection = MagicMock()
        pika_stub.ConnectionParameters = MagicMock()
        pika_stub.PlainCredentials = MagicMock()
        pika_stub.exceptions = types.SimpleNamespace(
            AMQPConnectionError=Exception, ChannelClosedByBroker=Exception,
        )
        sys.modules["pika"] = pika_stub
    # Avoid create_app() trying to reach a real DB at import time.
    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    # Drop any cached version so each test starts from a fresh module state.
    sys.modules.pop("token_issuer_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "token_issuer_under_test",
        os.path.join(ROOT, "services", "token-issuer", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client():
    """Return a Flask test client + SQLite-backed SessionLocal wired into
    the freshly loaded token-issuer module."""
    mod = _load_token_issuer()
    # Only create the MeetingBrief table on SQLite: other InternalBase
    # tables use Postgres-specific UUID types that the SQLite dialect can't
    # render. We don't need them for brief-CRUD tests.
    from libs.shared.app.models import InternalBase, MeetingBrief  # noqa: WPS433
    # SQLite doesn't render postgres UUID — but MeetingBrief uses uuid pk too.
    # Patch the table column types at runtime for the test engine only.
    from sqlalchemy import String
    import uuid as _uuid
    for col in MeetingBrief.__table__.columns:
        if col.name == "id":
            col.type = String(36)
            col.default.arg = lambda _ctx=None: str(_uuid.uuid4())
    engine = create_engine("sqlite:///:memory:")
    MeetingBrief.__table__.create(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    mod.SessionLocal = Session
    mod.app.config["TESTING"] = True
    return mod.app.test_client(), mod


def _auth():
    return {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}


def _create(c, **fields):
    payload = {
        "user_sub": "user-a",
        "subject": "Sujet alpha",
        "drive_folder_id": "folder-1",
        "role": "animateur",
        "expectation": "préparer le ROI",
        "focus": ["risques", "décisions"],
        "duration_minutes": 60,
        "brief_json": {"executive_summary": "S", "risks": ["r1"]},
        "documents": [{"name": "doc.pdf", "status": "ingested"}],
        "title": "Titre alpha",
    }
    payload.update(fields)
    return c.post("/api/v1/briefs", json=payload, headers=_auth())


def test_create_persists_and_returns_brief_id(client):
    c, _ = client
    r = _create(c)
    assert r.status_code == 200, r.data
    body = r.get_json()
    assert body["ok"] is True
    brief = body["brief"]
    assert brief["id"]
    assert brief["subject"] == "Sujet alpha"
    assert brief["title"] == "Titre alpha"
    assert brief["brief_json"]["executive_summary"] == "S"


def test_list_filters_trashed_and_isolates_by_user_sub(client):
    c, _ = client
    # 2 briefs pour user-a, 1 pour user-b
    r1 = _create(c, subject="A1", title="A1")
    r2 = _create(c, subject="A2", title="A2")
    r3 = _create(c, user_sub="user-b", subject="B1", title="B1")
    assert r1.status_code == r2.status_code == r3.status_code == 200

    # Listing user-a → 2 briefs
    r = c.get("/api/v1/briefs?user_sub=user-a", headers=_auth())
    assert r.status_code == 200
    briefs = r.get_json()["briefs"]
    assert {b["title"] for b in briefs} == {"A1", "A2"}

    # Listing user-b → 1 brief
    r = c.get("/api/v1/briefs?user_sub=user-b", headers=_auth())
    assert {b["title"] for b in r.get_json()["briefs"]} == {"B1"}

    # Trash one of user-a's briefs → listing actif ne le retourne plus
    bid = briefs[0]["id"]
    rd = c.delete(f"/api/v1/briefs/{bid}", json={"user_sub": "user-a"}, headers=_auth())
    assert rd.status_code == 200
    titles = {
        b["title"] for b in
        c.get("/api/v1/briefs?user_sub=user-a", headers=_auth()).get_json()["briefs"]
    }
    assert briefs[0]["title"] not in titles
    # Trashed listing renvoie bien le brief
    trashed = c.get("/api/v1/briefs?user_sub=user-a&trashed=true", headers=_auth()).get_json()["briefs"]
    assert {b["title"] for b in trashed} == {briefs[0]["title"]}


def test_delete_then_restore_round_trip(client):
    c, _ = client
    bid = _create(c).get_json()["brief"]["id"]
    # Trash
    r = c.delete(f"/api/v1/briefs/{bid}", json={"user_sub": "user-a"}, headers=_auth())
    assert r.status_code == 200 and r.get_json()["trashed"]
    # Get → 404 (trashed)
    r = c.get(f"/api/v1/briefs/{bid}?user_sub=user-a", headers=_auth())
    assert r.status_code == 404
    # Restore
    r = c.post(f"/api/v1/briefs/{bid}/restore", json={"user_sub": "user-a"}, headers=_auth())
    assert r.status_code == 200 and r.get_json()["restored"]
    # Get → 200
    r = c.get(f"/api/v1/briefs/{bid}?user_sub=user-a", headers=_auth())
    assert r.status_code == 200


def test_rename_changes_title_only(client):
    c, _ = client
    bid = _create(c).get_json()["brief"]["id"]
    r = c.post(
        f"/api/v1/briefs/{bid}/rename",
        json={"user_sub": "user-a", "title": "Nouveau titre"},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.get_json()["title"] == "Nouveau titre"
    # Le subject reste intact
    b = c.get(f"/api/v1/briefs/{bid}?user_sub=user-a", headers=_auth()).get_json()["brief"]
    assert b["title"] == "Nouveau titre"
    assert b["subject"] == "Sujet alpha"


def test_amend_modifies_brief_json_only(client):
    c, _ = client
    created = _create(c).get_json()["brief"]
    bid = created["id"]
    new_payload = {"executive_summary": "réécrit", "risks": ["r1", "r2"]}
    r = c.post(
        f"/api/v1/briefs/{bid}/amend",
        json={"user_sub": "user-a", "brief_json": new_payload},
        headers=_auth(),
    )
    assert r.status_code == 200
    b = r.get_json()["brief"]
    assert b["brief_json"] == new_payload
    # Les autres champs n'ont pas bougé.
    assert b["subject"] == created["subject"]
    assert b["title"] == created["title"]
    assert b["role"] == created["role"]
    assert b["duration_minutes"] == created["duration_minutes"]
    assert b["focus"] == created["focus"]


def test_404_for_inexistent_or_wrong_user_sub(client):
    c, _ = client
    bid = _create(c).get_json()["brief"]["id"]
    # mauvais user_sub → 404 même si l'id existe
    r = c.get(f"/api/v1/briefs/{bid}?user_sub=user-x", headers=_auth())
    assert r.status_code == 404
    r = c.post(
        f"/api/v1/briefs/{bid}/rename",
        json={"user_sub": "user-x", "title": "Hack"}, headers=_auth(),
    )
    assert r.status_code == 404
    # id inexistant
    fake = "00000000-0000-0000-0000-000000000000"
    r = c.get(f"/api/v1/briefs/{fake}?user_sub=user-a", headers=_auth())
    assert r.status_code == 404


def test_auth_required(client):
    c, _ = client
    r = c.get("/api/v1/briefs?user_sub=user-a")
    assert r.status_code == 401
