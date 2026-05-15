"""Unit tests for meeting-prep v2 endpoints exposed by token-issuer.

Couvre :
  - POST /api/v1/files/by-id/link-brief : isolation user_sub, existence du
    brief cible, mise à jour effective.
  - GET /api/v1/briefs/<id>/audio-files : isolation, sérialisation.
  - GET /api/v1/briefs/<id>/series : remontée + redescente chaîne.
  - GET /api/v1/briefs/list-with-counts : linked_audio_count +
    older_than_90d_unlinked_count.
  - POST /api/v1/user-glossary/upsert-batch : insert / bump / skip
    blacklisted.
  - GET /api/v1/user-glossary : tri freq desc.
"""

import importlib.util
import os
import sys
import types
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine, String
from sqlalchemy.orm import sessionmaker


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_INTERNAL_TOKEN = "***REMOVED-FIXTURE-TOKEN***"


def _load_token_issuer():
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name in ("libs.shared", "libs"):
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)
    if "pika" not in sys.modules:
        pika_stub = types.ModuleType("pika")
        pika_stub.BlockingConnection = MagicMock()
        pika_stub.ConnectionParameters = MagicMock()
        pika_stub.PlainCredentials = MagicMock()
        pika_stub.exceptions = types.SimpleNamespace(
            AMQPConnectionError=Exception, ChannelClosedByBroker=Exception,
        )
        sys.modules["pika"] = pika_stub
    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    sys.modules.pop("token_issuer_v2_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "token_issuer_v2_under_test",
        os.path.join(ROOT, "services", "token-issuer", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app_and_db():
    mod = _load_token_issuer()
    from libs.shared.app.models import MeetingBrief, UserAudioFile, UserGlossaryTerm

    # Patch UUID pk → CHAR(36) pour SQLite (idem fixture v1).
    for tbl_cls in (MeetingBrief, UserAudioFile):
        for col in tbl_cls.__table__.columns:
            if col.name == "id":
                col.type = String(36)
                if col.default is not None:
                    col.default.arg = lambda _ctx=None: str(uuid.uuid4())

    engine = create_engine("sqlite:///:memory:")
    MeetingBrief.__table__.create(engine)
    UserAudioFile.__table__.create(engine)
    UserGlossaryTerm.__table__.create(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    mod.SessionLocal = Session
    mod.app.config["TESTING"] = True
    return mod.app.test_client(), mod, Session


def _auth():
    return {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}


def _mk_brief(Session, user_sub="user-a", subject="Sujet alpha",
              created_at=None, series_parent_id=None):
    from libs.shared.app.models import MeetingBrief
    db = Session()
    b = MeetingBrief(
        user_sub=user_sub,
        subject=subject,
        title=subject,
        created_at=created_at or datetime.now(timezone.utc),
        series_parent_id=series_parent_id,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    bid = str(b.id)
    db.close()
    return bid


def _mk_audio(Session, user_sub="user-a", brief_id=None, filename="r.m4a"):
    from libs.shared.app.models import UserAudioFile
    db = Session()
    a = UserAudioFile(
        user_sub=user_sub,
        original_session_code="ABCD123",
        original_filename=filename,
        stored_filename="stored.m4a",
        file_size_bytes=1234,
        meeting_brief_id=brief_id,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    aid = str(a.id)
    db.close()
    return aid


# ─── link-brief ─────────────────────────────────────────────


def test_link_brief_updates_meeting_brief_id(app_and_db):
    client, mod, Session = app_and_db
    brief_id = _mk_brief(Session)
    audio_id = _mk_audio(Session)
    resp = client.post(
        "/api/v1/files/by-id/link-brief",
        json={"user_sub": "user-a", "file_id": audio_id, "meeting_brief_id": brief_id},
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data["ok"] is True
    assert data["new_brief_id"] == brief_id


def test_link_brief_detaches_when_meeting_brief_id_is_null(app_and_db):
    client, mod, Session = app_and_db
    brief_id = _mk_brief(Session)
    audio_id = _mk_audio(Session, brief_id=brief_id)
    resp = client.post(
        "/api/v1/files/by-id/link-brief",
        json={"user_sub": "user-a", "file_id": audio_id, "meeting_brief_id": None},
        headers=_auth(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["new_brief_id"] is None


def test_link_brief_rejects_brief_owned_by_other_user(app_and_db):
    client, mod, Session = app_and_db
    other_brief = _mk_brief(Session, user_sub="user-b")
    audio_id = _mk_audio(Session, user_sub="user-a")
    resp = client.post(
        "/api/v1/files/by-id/link-brief",
        json={"user_sub": "user-a", "file_id": audio_id, "meeting_brief_id": other_brief},
        headers=_auth(),
    )
    assert resp.status_code == 404


# ─── audio-files ────────────────────────────────────────────


def test_list_brief_audio_files_returns_linked_only(app_and_db):
    client, mod, Session = app_and_db
    brief_id = _mk_brief(Session)
    a1 = _mk_audio(Session, brief_id=brief_id, filename="lié-1.m4a")
    _ = _mk_audio(Session, brief_id=None, filename="non-lié.m4a")
    resp = client.get(
        f"/api/v1/briefs/{brief_id}/audio-files?user_sub=user-a",
        headers=_auth(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["audio_files"]) == 1
    assert data["audio_files"][0]["id"] == a1


# ─── series ─────────────────────────────────────────────────


def test_series_chain_root_only_returns_self(app_and_db):
    client, mod, Session = app_and_db
    brief_id = _mk_brief(Session)
    resp = client.get(
        f"/api/v1/briefs/{brief_id}/series?user_sub=user-a",
        headers=_auth(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["root_id"] == brief_id
    assert len(data["series"]) == 1


def test_series_chain_parent_child(app_and_db):
    client, mod, Session = app_and_db
    parent = _mk_brief(Session, subject="Parent")
    child = _mk_brief(Session, subject="Child", series_parent_id=parent)
    resp = client.get(
        f"/api/v1/briefs/{child}/series?user_sub=user-a",
        headers=_auth(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["root_id"] == parent
    assert len(data["series"]) == 2
    assert data["series"][0]["id"] == parent
    assert data["series"][1]["id"] == child


# ─── list-with-counts ────────────────────────────────────────


def test_list_with_counts_returns_linked_audio_count(app_and_db):
    client, mod, Session = app_and_db
    brief_id = _mk_brief(Session)
    _mk_audio(Session, brief_id=brief_id)
    _mk_audio(Session, brief_id=brief_id)
    resp = client.get(
        "/api/v1/briefs/list-with-counts?user_sub=user-a",
        headers=_auth(),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    target = next(b for b in data["briefs"] if b["id"] == brief_id)
    assert target["linked_audio_count"] == 2


def test_list_with_counts_returns_older_than_90d_unlinked_count(app_and_db):
    client, mod, Session = app_and_db
    old_date = datetime.now(timezone.utc) - timedelta(days=120)
    _mk_brief(Session, subject="ancien sans audio", created_at=old_date)
    resp = client.get(
        "/api/v1/briefs/list-with-counts?user_sub=user-a",
        headers=_auth(),
    )
    data = resp.get_json()
    assert data["older_than_90d_unlinked_count"] >= 1


# ─── user glossary ──────────────────────────────────────────


def test_user_glossary_upsert_batch_inserts_and_bumps(app_and_db):
    client, mod, Session = app_and_db
    resp = client.post(
        "/api/v1/user-glossary/upsert-batch",
        json={"user_sub": "user-a", "terms": ["RGPD", "DTNUM"]},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.get_json()["inserted"] == 2

    # Deuxième batch : un nouveau, un existant.
    resp2 = client.post(
        "/api/v1/user-glossary/upsert-batch",
        json={"user_sub": "user-a", "terms": ["RGPD", "DGSI"]},
        headers=_auth(),
    )
    data = resp2.get_json()
    assert data["inserted"] == 1
    assert data["bumped"] == 1


def test_user_glossary_list_orders_by_frequency(app_and_db):
    client, mod, Session = app_and_db
    for _ in range(3):
        client.post(
            "/api/v1/user-glossary/upsert-batch",
            json={"user_sub": "user-a", "terms": ["FREQUENT"]},
            headers=_auth(),
        )
    client.post(
        "/api/v1/user-glossary/upsert-batch",
        json={"user_sub": "user-a", "terms": ["RARE"]},
        headers=_auth(),
    )
    resp = client.get("/api/v1/user-glossary?user_sub=user-a", headers=_auth())
    data = resp.get_json()
    terms = [t["term"] for t in data["terms"]]
    assert terms.index("FREQUENT") < terms.index("RARE")


def test_user_glossary_blacklist_via_delete_action(app_and_db):
    client, mod, Session = app_and_db
    client.post(
        "/api/v1/user-glossary/upsert-batch",
        json={"user_sub": "user-a", "terms": ["TO-DELETE"]},
        headers=_auth(),
    )
    resp = client.post(
        "/api/v1/user-glossary/term/TO-DELETE",
        json={"user_sub": "user-a", "action": "delete"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    # Le terme ne doit plus apparaître dans la liste.
    resp = client.get("/api/v1/user-glossary?user_sub=user-a", headers=_auth())
    terms = [t["term"] for t in resp.get_json()["terms"]]
    assert "TO-DELETE" not in terms
