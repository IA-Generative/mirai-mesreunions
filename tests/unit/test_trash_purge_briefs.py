"""
Unit tests for the brief-side purge balayage.

L'endpoint interne ``POST /api/v1/briefs/purge`` est appelé par
code-generator ``_purge_expired_trash`` (zone externe) avec
``older_than_days = TRASH_RETENTION_DAYS`` (30 par défaut). Cette suite
vérifie son contrat :

  - un brief trashed depuis 31 jours est hard-deleted ;
  - un brief trashed depuis < 30 jours est conservé ;
  - le balayage est idempotent (un 2ᵉ appel renvoie purged=0 sans casser).
"""

import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_INTERNAL_TOKEN = "***REMOVED-FIXTURE-TOKEN***"


def _load_token_issuer():
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name == "libs.shared" or name == "libs":
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)
    if "pika" not in sys.modules or not hasattr(sys.modules["pika"], "BlockingConnection"):
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
    sys.modules.pop("token_issuer_under_purge", None)
    spec = importlib.util.spec_from_file_location(
        "token_issuer_under_purge",
        os.path.join(ROOT, "services", "token-issuer", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def harness():
    """Token-issuer Flask client + live SQLite session for direct row setup."""
    mod = _load_token_issuer()
    from libs.shared.app.models import MeetingBrief  # noqa: WPS433
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
    return mod.app.test_client(), Session, MeetingBrief


def _auth():
    return {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}


def test_brief_trashed_31_days_ago_is_hard_deleted(harness):
    c, Session, MeetingBrief = harness
    db = Session()
    old = MeetingBrief(
        user_sub="user-a",
        subject="Old",
        title="Old",
        trashed_at=datetime.now(timezone.utc) - timedelta(days=31),
    )
    db.add(old)
    db.commit()
    old_id = old.id
    db.close()

    r = c.post(
        "/api/v1/briefs/purge",
        json={"user_sub": "user-a", "older_than_days": 30},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.get_json()["purged"] == 1

    # 2e appel : idempotent — purged=0.
    r = c.post(
        "/api/v1/briefs/purge",
        json={"user_sub": "user-a", "older_than_days": 30},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.get_json()["purged"] == 0

    # La row n'existe plus.
    db = Session()
    assert db.query(MeetingBrief).filter(MeetingBrief.id == old_id).first() is None
    db.close()


def test_brief_trashed_recently_is_kept(harness):
    c, Session, MeetingBrief = harness
    db = Session()
    recent = MeetingBrief(
        user_sub="user-a",
        subject="Recent",
        title="Recent",
        trashed_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    db.add(recent)
    db.commit()
    rid = recent.id
    db.close()

    r = c.post(
        "/api/v1/briefs/purge",
        json={"user_sub": "user-a", "older_than_days": 30},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert r.get_json()["purged"] == 0
    db = Session()
    assert db.query(MeetingBrief).filter(MeetingBrief.id == rid).first() is not None
    db.close()


def test_purge_is_isolated_by_user_sub(harness):
    c, Session, MeetingBrief = harness
    db = Session()
    for sub in ("user-a", "user-b"):
        db.add(MeetingBrief(
            user_sub=sub, subject=sub, title=sub,
            trashed_at=datetime.now(timezone.utc) - timedelta(days=60),
        ))
    db.commit()
    db.close()

    r = c.post(
        "/api/v1/briefs/purge",
        json={"user_sub": "user-a", "older_than_days": 30},
        headers=_auth(),
    )
    assert r.get_json()["purged"] == 1
    # user-b reste intact.
    db = Session()
    assert db.query(MeetingBrief).filter(MeetingBrief.user_sub == "user-b").count() == 1
    db.close()
