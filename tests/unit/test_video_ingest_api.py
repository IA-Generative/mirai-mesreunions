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


# ─── GET /video/my-bookmarks ──────────────────────────────────────────

def test_my_bookmarks_empty(client):
    cur = MagicMock()
    cur.fetchall.return_value = []
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/my-bookmarks")
    assert resp.status_code == 200
    assert resp.get_json() == {"bookmarks": []}


def test_my_bookmarks_returns_rich_payload(client):
    from datetime import datetime, timezone
    cur = MagicMock()
    cur.fetchall.return_value = [
        (
            42, 1, datetime(2026, 5, 26, tzinfo=timezone.utc), "meeting", "m-9",
            "youtube", "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "Test Video", "Test Channel", 245,
            "fr", 12345, "subtitle_manual",
        ),
    ]
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/my-bookmarks")
    body = resp.get_json()
    assert resp.status_code == 200
    assert len(body["bookmarks"]) == 1
    b = body["bookmarks"][0]
    assert b["video_source_id"] == 1
    assert b["title"] == "Test Video"
    assert b["transcript_language"] == "fr"
    assert b["transcript_chars"] == 12345
    assert b["has_transcript"] is True


def test_my_bookmarks_no_transcript_yet(client):
    from datetime import datetime, timezone
    cur = MagicMock()
    cur.fetchall.return_value = [
        (
            42, 1, datetime(2026, 5, 26, tzinfo=timezone.utc), None, None,
            "youtube", "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "Test Video", "Test Channel", 245,
            None, None, None,
        ),
    ]
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/my-bookmarks")
    b = resp.get_json()["bookmarks"][0]
    assert b["has_transcript"] is False
    assert b["transcript_language"] is None


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
        None,  # next_attempt_at — NULL sur un job terminé
    )
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/jobs/1")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "done"
    assert body["video_source_id"] == 99
    assert body["retrying"] is False


def test_get_job_en_backoff_expose_retrying(client):
    """Job replacé en pending après un anti-bot YouTube : le front doit
    pouvoir distinguer « ça retente » de « ça tourne »."""
    from datetime import datetime
    cur = MagicMock()
    cur.fetchone.return_value = (
        31, "pending", None, None, "transient (tentative 1/4, retry dans 20s)", 1,
        datetime(2026, 8, 2), None, "dev-anon",
        datetime(2026, 8, 2, 10, 0, 20),
    )
    cm = MagicMock(); cm.__enter__.return_value = cur; cm.__exit__.return_value = False
    with patch("services.video_ingest.app.api.db.cursor", return_value=cm):
        resp = client.get("/video/jobs/31")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["retrying"] is True
    assert body["next_attempt_at"] == "2026-08-02T10:00:20"


# ─── GET /video/sources/<id>/transcript ───────────────────────────────
# Le catalogue est un cache partagé : la lecture est scopée par propriété
# (signet ou job de l'utilisateur). On patch `user_owns_source` pour
# distinguer le propriétaire (True) de l'énumérateur tiers (False).


def test_get_transcript_format_text(client):
    conn, cur, cm = _conn()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "hello world",
                                  [{"start_seconds": 0, "end_seconds": 5, "text": "hello world"}])
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=True):
        resp = client.get("/video/sources/1/transcript")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["text"] == "hello world"
    assert "segments" not in body
    assert "markdown" not in body


def test_get_transcript_format_segments(client):
    conn, cur, cm = _conn()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "x",
                                  [{"start_seconds": 0, "end_seconds": 5, "text": "x"}])
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=True):
        resp = client.get("/video/sources/1/transcript?format=segments")
    body = resp.get_json()
    assert body["segments"] == [{"start_seconds": 0, "end_seconds": 5, "text": "x"}]


def test_get_transcript_format_markdown_has_timestamps(client):
    conn, cur, cm = _conn()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "x",
                                  [{"start_seconds": 12, "end_seconds": 20, "text": "bonjour"},
                                   {"start_seconds": 20, "end_seconds": 30, "text": "monde"}])
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=True):
        resp = client.get("/video/sources/1/transcript?format=markdown")
    body = resp.get_json()
    assert "**[12s]**" in body["markdown"]
    assert "**[20s]**" in body["markdown"]


def test_get_transcript_not_found(client):
    conn, cur, cm = _conn()
    cur.fetchone.return_value = None
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=True):
        resp = client.get("/video/sources/999/transcript")
    assert resp.status_code == 404


def test_get_transcript_not_owned_returns_404(client):
    """BOLA : un utilisateur sans lien vers la source ne lit pas son transcript."""
    conn, cur, cm = _conn()
    cur.fetchone.return_value = (1, "fr", "subtitle_manual", "secret", [])
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=False):
        resp = client.get("/video/sources/1/transcript")
    assert resp.status_code == 404
    # La requête transcript ne doit même pas être exécutée.
    assert cur.execute.call_count == 0


def test_get_source_owner_ok(client):
    conn, cur, cm = _conn()
    from datetime import datetime
    cur.fetchone.return_value = (
        1, "youtube", "dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "Title", "Channel", 213, datetime(2026, 1, 1), {}, datetime(2026, 1, 2),
    )
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=True):
        resp = client.get("/video/sources/1")
    assert resp.status_code == 200
    assert resp.get_json()["title"] == "Title"


def test_get_source_not_owned_returns_404(client):
    """BOLA : énumération du catalogue partagé fermée pour un tiers."""
    conn, cur, cm = _conn()
    cur.fetchone.return_value = (
        1, "youtube", "x", "u", "Secret Title", "Secret Channel",
        1, None, {}, None,
    )
    with patch("services.video_ingest.app.api.db.connection", return_value=cm), \
         patch("services.video_ingest.app.api.user_owns_source", return_value=False):
        resp = client.get("/video/sources/1")
    assert resp.status_code == 404
    assert cur.execute.call_count == 0


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
