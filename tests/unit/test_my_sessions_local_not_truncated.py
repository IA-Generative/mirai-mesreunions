"""Régression « fichier uploadé invisible dans la liste » (incident 2026-06-11).

Symptôme : un upload depuis l'interface web réussit (S3 + publish AV scan +
``db.commit()``), le fichier est en base avec ``status=TRANSFERRED``, mais il
n'apparaît jamais dans ``/api/my-sessions``.

Cause racine : ``api_my_sessions`` cappait les sessions à 200 en triant par
``UploadSession.created_at DESC``. Or une session « Upload local » (préfixe
``L-``) est créée UNE fois puis réutilisée à chaque upload — son
``created_at`` reste figé alors que ses fichiers sont récents. Chez un
utilisateur à 337 sessions, la session locale (créée 3 jours plus tôt) était
au rang 336 → tronquée → tous ses fichiers invisibles.

Le fix (``_select_listed_sessions``) :
  1. cappe par ACTIVITÉ récente (``updated_at``, bumpé par ``upload_count += 1``)
     et non par ``created_at`` ;
  2. garantit que les sessions locales ne sont JAMAIS tronquées par le cap.

Ces deux garanties sont verrouillées indépendamment ci-dessous.
"""

import datetime
import os
import sys
import uuid

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
_SVC_DIR = os.path.join(ROOT, "services", "mesreunions-web")
if _SVC_DIR not in sys.path:
    sys.path.insert(0, _SVC_DIR)

pytest.importorskip("sqlalchemy")
os.environ.setdefault("INTERNAL_API_TOKEN", "x" * 48)


def _force_real_modules():
    """Neutralise les stubs ``sys.modules`` laissés par des tests voisins.

    Plusieurs unités (test_dmz_to_internal_bridge_publish, test_loudnorm…)
    remplacent ``libs.shared.app.models`` / ``.config`` par des ModuleType
    stub (sans ``__file__``) et ne nettoient pas. On les retire pour forcer
    le rechargement des vrais modules, et on drop ``app.*`` pour que
    routes/service se relient aux vrais ``libs``.
    """
    for name in list(sys.modules):
        if name == "libs" or name.startswith("libs.shared") or name == "app" or name.startswith("app."):
            mod = sys.modules.get(name)
            if name == "app" or name.startswith("app.") or (mod is not None and not getattr(mod, "__file__", None)):
                sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def _clean_imports():
    _force_real_modules()
    yield


def _now():
    return datetime.datetime(2026, 6, 11, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def routes():
    # Import direct : le module s'importe proprement (boto3/pika installés),
    # seul INTERNAL_API_TOKEN est requis (réglé ci-dessus).
    import app.modules.sessions.routes as r
    return r


@pytest.fixture
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from libs.shared.app.models import ExternalBase
    engine = create_engine("sqlite://")
    ExternalBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _mk_session(db, *, code, created, updated, user="u1"):
    from libs.shared.app.models import UploadSession, SessionStatus
    s = UploadSession(
        id=uuid.uuid4(),
        user_sub=user,
        simple_code=code,
        qr_token=uuid.uuid4().hex,
        status=SessionStatus.ACTIVE,
        max_uploads=9999,
        upload_count=1,
        ttl_minutes=0,
        created_at=created,
        updated_at=updated,
        expires_at=_now(),
    )
    db.add(s)
    return s


def test_local_session_active_survives_cap_via_recency(routes, db, monkeypatch):
    """Session locale ANCIENNE (created) mais ACTIVE (updated récent) : le tri
    par activité doit la faire remonter au-dessus du cap."""
    monkeypatch.setattr(routes, "_MY_SESSIONS_CAP", 5)
    day0 = datetime.datetime(2026, 6, 8, tzinfo=datetime.timezone.utc)
    # Session locale : created il y a 3 jours, mais ré-incrémentée à l'instant.
    local = _mk_session(db, code="L-OLD", created=day0, updated=_now())
    # 10 sessions QR plus récentes en created_at (rang local = 10 par created),
    # mais aucune re-touchée depuis → updated == created (toutes < now).
    for i in range(10):
        ts = day0 + datetime.timedelta(days=1, hours=i)
        _mk_session(db, code=f"QR-{i}", created=ts, updated=ts)
    db.commit()

    selected = routes._select_listed_sessions(db, "u1")
    codes = [s.simple_code for s in selected]
    assert "L-OLD" in codes, "session locale active tronquée par le cap (régression)"
    # Le cap reste effectif sur les sessions QR (pas de listing illimité).
    assert sum(1 for c in codes if c.startswith("QR-")) <= 5


def test_local_session_inactive_always_included(routes, db, monkeypatch):
    """Session locale INACTIVE (created ET updated anciens) : exclue par le
    cap de récence, mais garantie incluse par le filet « locales toujours »."""
    monkeypatch.setattr(routes, "_MY_SESSIONS_CAP", 5)
    day0 = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    local = _mk_session(db, code="L-COLD", created=day0, updated=day0)
    for i in range(10):
        ts = datetime.datetime(2026, 6, 5, tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=i)
        _mk_session(db, code=f"QR-{i}", created=ts, updated=ts)
    db.commit()

    selected = routes._select_listed_sessions(db, "u1")
    codes = [s.simple_code for s in selected]
    assert "L-COLD" in codes, "session locale inactive doit rester listée (filet de sécurité)"
    # Pas de doublon si une locale figurait déjà dans le top-cap.
    assert codes.count("L-COLD") == 1


def test_other_users_isolated(routes, db, monkeypatch):
    """Le helper ne fuit pas les sessions d'un autre user."""
    monkeypatch.setattr(routes, "_MY_SESSIONS_CAP", 5)
    _mk_session(db, code="L-U2", created=_now(), updated=_now(), user="u2")
    db.commit()
    selected = routes._select_listed_sessions(db, "u1")
    assert selected == []
