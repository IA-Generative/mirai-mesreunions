"""Tests du store de jobs de génération de brief (generation_jobs.py).

Couvre le filet ADR-0001 « liveness vs progress » ajouté après l'incident
« la préparation crashe à la fin » (2026-08) :

  - un job orphelin (worker thread tué par un restart de pod) est
    requalifié ``failed`` à la lecture au-delà du seuil de staleness,
    au lieu de laisser le front poller indéfiniment ;
  - un job frais ou terminé n'est pas touché ;
  - la requalification est persistée (le 2e poll relit ``failed``).

Backend : SQLite in-memory branché sur le runtime du module (pattern
session_factory injecté via app.runtime.configure_runtime).
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
WEB_DIR = os.path.join(ROOT, "services", "mesreunions-web")
if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from libs.shared.app.models import ExternalBase, PreparationGenerationJob


@pytest.fixture
def jobs_module(monkeypatch):
    """Charge generation_jobs avec une session factory SQLite in-memory."""
    os.environ.setdefault("INTERNAL_API_TOKEN", "x" * 40)
    engine = create_engine("sqlite://")
    ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    from app import runtime
    from app.modules.preparations import generation_jobs

    monkeypatch.setitem(runtime._state, "session_factory", factory)
    yield generation_jobs, factory
    engine.dispose()


def _insert_job(factory, *, phase, started_ago_seconds, finished=False):
    db = factory()
    now = datetime.now(timezone.utc)
    row = PreparationGenerationJob(
        id="job-under-test",
        user_sub="sub-1",
        phase=phase,
        docs_processed=0,
        docs_total=0,
        started_at=now - timedelta(seconds=started_ago_seconds),
        finished_at=(now if finished else None),
    )
    db.add(row)
    db.commit()
    db.close()
    return "job-under-test"


def test_stale_unfinished_job_is_requalified_failed(jobs_module):
    """Job en phase intermédiaire depuis > seuil → failed + message actionnable."""
    generation_jobs, factory = jobs_module
    job_id = _insert_job(factory, phase="generating_llm",
                         started_ago_seconds=generation_jobs._STALE_AFTER_SECONDS + 60)

    view = generation_jobs.get_job(job_id, "sub-1")

    assert view is not None
    assert view["phase"] == "failed"
    assert "brouillon" in (view["error"] or "")
    # Persisté : une relecture directe voit le même état terminal.
    again = generation_jobs.get_job(job_id, "sub-1")
    assert again["phase"] == "failed"
    assert again["finished_at"] is not None


def test_fresh_unfinished_job_is_untouched(jobs_module):
    generation_jobs, factory = jobs_module
    job_id = _insert_job(factory, phase="generating_llm", started_ago_seconds=60)

    view = generation_jobs.get_job(job_id, "sub-1")

    assert view["phase"] == "generating_llm"
    assert view["error"] is None
    assert view["finished_at"] is None


def test_finished_job_is_never_requalified(jobs_module):
    """Un job done très vieux reste done (finished_at posé → hors périmètre)."""
    generation_jobs, factory = jobs_module
    job_id = _insert_job(factory, phase="done",
                         started_ago_seconds=generation_jobs._STALE_AFTER_SECONDS * 10,
                         finished=True)

    view = generation_jobs.get_job(job_id, "sub-1")

    assert view["phase"] == "done"


def test_get_job_isolation_other_user(jobs_module):
    generation_jobs, factory = jobs_module
    job_id = _insert_job(factory, phase="queued", started_ago_seconds=10)

    assert generation_jobs.get_job(job_id, "someone-else") is None
