"""Tests de l'API REST — Flask test client + mocks BDD.

Couvre :
- auth bypass via VIDEO_INGEST_AUTH_DISABLED en dev
- POST /import : URL invalide, dédup HIT (reused=true sync), MISS (enqueue)
- GET /jobs/<id> : owner check
- GET /sources/<id> : 404 + nominal
- GET /sources/<id>/transcript : formats text / segments / markdown
- GET /search : 400 sans q, résultats rangés
- DELETE /sources/<id> : require admin
- /health pas d'auth
"""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _dev_auth(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_AUTH_DISABLED", "1")
    yield


@pytest.fixture
def client():
    from services.video_ingest.app.api import create_app
    app = create_app()
    app.testing = True
    return app.test_client()


def _conn():
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return conn, cur, cm


def test_health_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


# ─── POST /video/import ────────────────────────────────────────────────

def test_import_missing_url_returns_400(client):
    resp = client.post("/video/import", json={})
    assert resp.status_code == 400


def test_import_unroutable_url_returns_400(client):
    resp = client.post("/video/import", json={"url": "https://vimeo.com/123"})
    assert resp.status_code == 400


def test_import_cache_hit_returns_ready_sync(client):
    conn, cur, cm = _conn()
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.find_source_by_provider_id", return_value=777), \
         patch("services.video_ingest.app.api.has_transcript", return_value=True), \
         patch("services.video_ingest.app.api.add_bookmark", return_value=999), \
         patch("services.video_ingest.app.api.audit.log_event"):
        resp = client.post("/video/import", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ready"
    assert body["reused"] is True
    assert body["video_source_id"] == 777
    assert body["bookmark_id"] == 999
    assert body["job_id"] is None


def test_import_cache_miss_enqueues_job(client):
    conn, cur, cm = _conn()
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.find_source_by_provider_id", return_value=None), \
         patch("services.video_ingest.app.api.has_transcript", return_value=False), \
         patch("services.video_ingest.app.api.quotas.check_import_quota"), \
         patch("services.video_ingest.app.api.audit.log_event"), \
         patch("services.video_ingest.app.api.jobs_mod.enqueue", return_value=42):
        resp = client.post("/video/import", json={
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "language": "fr",
            "context": "meeting",
            "context_id": "m-9",
        })
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "pending"
    assert body["reused"] is False
    assert body["job_id"] == 42


# ─── GET /video/jobs/<id> ──────────────────────────────────────────────

def test_get_job_not_found(client):
    conn, cur, cm = _conn()
    cur.fetchone.return_value = None
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm) as m:
        # `db.cursor` is itself a context manager: wrap it
        m.return_value.__enter__.return_value = cur
        resp = client.get("/video/jobs/1")
    assert resp.status_code == 404


def test_get_job_owner_mismatch_returns_404(client):
    cur = MagicMock()
    cur.fetchone.return_value = (
        1, "pending", None, None, None, 0, None, None, "someone-else",
    )
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/jobs/1")
    assert resp.status_code == 404  # not 403, pas d'information sur l'existence


def test_get_job_nominal(client):
    from datetime import datetime
    cur = MagicMock()
    cur.fetchone.return_value = (
        1, "done", 99, False, None, 1,
        datetime(2026, 5, 25), datetime(2026, 5, 25), "dev-anon",
    )
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/jobs/1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "done"
    assert body["video_source_id"] == 99


# ─── GET /video/sources/<id>/transcript ───────────────────────────────

def test_get_transcript_format_text(client):
    cur = MagicMock()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "hello world",
                                  [{"start_seconds": 0, "end_seconds": 5, "text": "hello world"}])
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/sources/1/transcript")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["text"] == "hello world"
    assert "segments" not in body
    assert "markdown" not in body


def test_get_transcript_format_segments(client):
    cur = MagicMock()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "x",
                                  [{"start_seconds": 0, "end_seconds": 5, "text": "x"}])
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/sources/1/transcript?format=segments")
    body = resp.get_json()
    assert body["segments"] == [{"start_seconds": 0, "end_seconds": 5, "text": "x"}]


def test_get_transcript_format_markdown_has_timestamps(client):
    cur = MagicMock()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "x",
                                  [{"start_seconds": 12, "end_seconds": 20, "text": "bonjour"},
                                   {"start_seconds": 20, "end_seconds": 30, "text": "monde"}])
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/sources/1/transcript?format=markdown")
    body = resp.get_json()
    assert "**[12s]**" in body["markdown"]
    assert "**[20s]**" in body["markdown"]


def test_get_transcript_not_found(client):
    cur = MagicMock()
    cur.fetchone.return_value = None
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/sources/999/transcript")
    assert resp.status_code == 404


# ─── GET /video/search ────────────────────────────────────────────────

def test_search_requires_q(client):
    resp = client.get("/video/search")
    assert resp.status_code == 400


def test_search_returns_ranked_results(client):
    cur = MagicMock()
    cur.fetchall.return_value = [
        (1, "Conf A", "https://youtu.be/aaa", "fr", 0.92, "…fragment <b>match</b>…"),
        (2, "Conf B", "https://youtu.be/bbb", "fr", 0.51, "…autre <b>match</b>…"),
    ]
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/search?q=keycloak")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["query"] == "keycloak"
    assert len(body["results"]) == 2
    assert body["results"][0]["rank"] >= body["results"][1]["rank"]


# ─── DELETE /video/sources/<id> (admin) ───────────────────────────────

def test_purge_requires_admin_role(client):
    # En mode dev bypass on n'a pas le rôle admin → 403
    resp = client.delete("/video/sources/1")
    assert resp.status_code == 403


# NB : la logique du rôle admin est testée dans test_video_ingest_auth.py.
# Ici on ne valide que le contrat HTTP : sans le rôle, 403.
