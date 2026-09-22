"""Bout en bout : un import YouTube qui échoue le dit, au lieu de rester
« en cours » pour toujours.

Constaté en production : des réunions-placeholder vieilles de 40 jours,
journalisées « stale … materialize hook muet ? » à chaque ouverture de
l'onglet, alors que la file video-ingest avait rendu son verdict (job
``failed`` : YouTube 403, vidéo privée…). La liste ne lisait que l'UAF.

Système sous test : ``mesreunions-web`` (Flask, code réel). Les deux
services qu'il appelle — device-token-authority et video-ingest — sont
de faux serveurs HTTP locaux, joints par la vraie bibliothèque ``requests``.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("flask")
pytest.importorskip("requests")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.e2e._fakes import FakeService  # noqa: E402
from tests.unit.test_meeting_prep_route import _INTERNAL_TOKEN, _load_mesreunions_web, _login  # noqa: E402

USER = "user-e2e-yt"
URL = "https://www.youtube.com/watch?v=dCANDyu23rc"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Authority:
    """Faux device-token-authority : mémoire de meetings par utilisateur."""

    def __init__(self):
        self.meetings: list[dict] = []
        self._n = 0

    def add(self, *, created_at: datetime, vsid=None, job_id=None, audio_preview=None):
        self._n += 1
        m = {"id": f"m-{self._n}", "user_sub": USER, "title": URL,
             "created_at": _iso(created_at), "video_source_id": vsid,
             "video_ingest_job_id": job_id, "user_audio_file_id": None,
             "audio_preview": audio_preview or {}}
        self.meetings.append(m)
        return m

    def __call__(self, req):
        if req.headers.get("Authorization") != f"Bearer {_INTERNAL_TOKEN}":
            return 401, {"error": "bad internal token"}
        if req.method == "POST" and req.path == "/api/v1/meetings":
            m = self.add(created_at=datetime.now(timezone.utc))
            m["title"] = req.body.get("title")
            return 201, {"meeting": m, "reused": False}
        if req.method == "GET" and req.path == "/api/v1/meetings":
            assert req.query.get("only_video") == "1"
            return 200, {"meetings": [m for m in self.meetings if m["user_sub"] == req.query.get("user_sub")]}
        if req.method == "PATCH" and req.path.endswith("/link-video"):
            mid = req.path.split("/")[-2]
            for m in self.meetings:
                if m["id"] == mid:
                    m["video_source_id"] = req.body.get("video_source_id")
                    m["video_ingest_job_id"] = req.body.get("video_ingest_job_id")
                    return 200, {"meeting": m}
            return 404, {"error": "no such meeting"}
        return 404, {"error": f"unexpected {req.method} {req.path}"}


class _VideoIngest:
    """Faux video-ingest : une file de jobs dont on fixe le verdict."""

    def __init__(self):
        self.jobs: dict[int, dict] = {}
        self.next_id = 41
        self.down = False

    def job(self, status, *, error=None, retrying=False, next_attempt_at=None, vsid=None):
        jid = self.next_id
        self.next_id += 1
        self.jobs[jid] = {"id": jid, "status": status, "video_source_id": vsid,
                          "reused": False, "error_message": error, "attempts": 1,
                          "created_at": _iso(datetime.now(timezone.utc)),
                          "completed_at": None, "next_attempt_at": next_attempt_at,
                          "retrying": retrying}
        return jid

    def __call__(self, req):
        if req.headers.get("Authorization") != "Bearer user-access-token":
            return 401, {"error": "bad bearer"}
        if req.method == "POST" and req.path == "/video/import":
            jid = self.job("pending")
            return 202, {"job_id": jid, "status": "pending", "reused": False}
        if req.method == "GET" and req.path == "/video/my-bookmarks":
            return 200, {"bookmarks": [
                {"video_source_id": j["video_source_id"], "title": "Titre YouTube",
                 "channel": "Chaîne", "duration_sec": 600, "canonical_url": URL}
                for j in self.jobs.values() if j["video_source_id"]]}
        if req.method == "GET" and req.path.startswith("/video/jobs/"):
            j = self.jobs.get(int(req.path.rsplit("/", 1)[1]))
            return (200, j) if j else (404, {"error": "job introuvable"})
        return 404, {"error": f"unexpected {req.method} {req.path}"}


@pytest.fixture
def stack(monkeypatch):
    authority, ingest = _Authority(), _VideoIngest()
    a_srv, v_srv = FakeService(authority).start(), FakeService(ingest).start()
    monkeypatch.setenv("TOKEN_ISSUER_INTERNAL_BASE_URL", a_srv.url)
    monkeypatch.setenv("VIDEO_INGEST_BASE_URL", v_srv.url)
    mod = _load_mesreunions_web()
    mod.app.config["TESTING"] = True
    routes = sys.modules["app.modules.youtube_import.routes"]
    routes._terminal_jobs.clear()
    # Le jeton OIDC de l'utilisateur vit normalement dans le magasin de
    # jetons (base) ; ici on le sert directement.
    monkeypatch.setattr(routes.token_store, "load_tokens",
                        lambda *a, **k: {"access_token": "user-access-token"})
    client = mod.app.test_client()
    _login(client, sub=USER)
    yield client, authority, ingest, a_srv, v_srv, routes
    a_srv.stop()
    v_srv.stop()


def _listing(client):
    r = client.get("/api/youtube/my-imports")
    assert r.status_code == 200, r.data
    return {it["meeting_id"]: it for it in r.get_json()["items"]}


def test_import_creates_placeholder_then_forwards_with_context(stack):
    client, authority, ingest, a_srv, v_srv, _ = stack
    r = client.post("/api/youtube/import", json={"url": URL})
    assert r.status_code == 202, r.data
    body = r.get_json()
    assert body["job_id"] == 41 and body["meeting_id"] == "m-1"
    (post,) = a_srv.calls("POST", "/api/v1/meetings")
    assert post.body["user_sub"] == USER and post.body["title"] == URL
    (fwd,) = v_srv.calls("POST", "/video/import")
    assert fwd.body["context"] == "meeting" and fwd.body["context_id"] == "m-1"
    assert fwd.body["url"] == URL


def test_failed_job_is_reported_with_its_cause(stack):
    client, authority, ingest, a_srv, v_srv, _ = stack
    jid = ingest.job("failed", error="provider_error: ERROR: unable to download video data: HTTP Error 403: Forbidden")
    m = authority.add(created_at=datetime.now(timezone.utc) - timedelta(days=40), vsid=32, job_id=jid)

    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "failed"
    assert it["transcription_status"] == "video_ingest_failed"
    assert it["job_status"] == "failed"
    assert it["job_error"].startswith("YouTube a refusé le téléchargement")
    assert "HTTP Error 403" in it["job_error"]
    # Un verdict rendu n'est pas un retard : plus de « matérialisation en retard ».
    assert it["stale"] is False


def test_private_video_gets_a_human_label(stack):
    client, authority, ingest, *_ = stack
    jid = ingest.job("failed", error="video_unavailable: Private video")
    m = authority.add(created_at=datetime.now(timezone.utc), job_id=jid)
    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "failed"
    assert it["job_error"].startswith("Vidéo indisponible")


def test_retrying_job_is_processing_with_next_attempt(stack):
    client, authority, ingest, *_ = stack
    nxt = _iso(datetime.now(timezone.utc) + timedelta(minutes=7))
    jid = ingest.job("pending", error="transient (tentative 1/4)", retrying=True, next_attempt_at=nxt)
    m = authority.add(created_at=datetime.now(timezone.utc) - timedelta(minutes=5), job_id=jid)
    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "processing"
    assert it["transcription_status"] == "video_ingest_retrying"
    assert it["job_next_attempt_at"] == nxt
    assert it["stale"] is False


def test_running_job_is_never_stale(stack):
    client, authority, ingest, *_ = stack
    jid = ingest.job("running")
    m = authority.add(created_at=datetime.now(timezone.utc) - timedelta(hours=3), job_id=jid)
    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "pending"
    assert it["stale"] is False


def test_done_job_without_uaf_is_the_real_stale_case(stack):
    """Job terminé mais aucun UAF : là, oui, le hook materialize s'est tu."""
    client, authority, ingest, *_ = stack
    jid = ingest.job("done", vsid=29)
    m = authority.add(created_at=datetime.now(timezone.utc) - timedelta(days=50), vsid=29, job_id=jid)
    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "pending"
    assert it["stale"] is True
    assert it["placeholder_age_seconds"] > 49 * 86400
    assert it["title"] == "Titre YouTube"


def test_uaf_present_wins_over_job_lookup(stack):
    """Dès que l'UAF existe, le pipeline interne fait foi : aucun appel
    à /video/jobs."""
    client, authority, ingest, a_srv, v_srv, _ = stack
    jid = ingest.job("failed", error="provider_error: late")
    m = authority.add(created_at=datetime.now(timezone.utc), vsid=5, job_id=jid,
                      audio_preview={"transcription_status": "kevent_partially_completed",
                                     "suggested_filename": "Comité projet"})
    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "done"
    assert it["title"] == "Comité projet"
    assert v_srv.calls("GET", "/video/jobs/") == []


def test_terminal_verdict_is_cached_between_listings(stack):
    client, authority, ingest, a_srv, v_srv, _ = stack
    failed = ingest.job("failed", error="provider_error: x")
    running = ingest.job("running")
    authority.add(created_at=datetime.now(timezone.utc), job_id=failed)
    authority.add(created_at=datetime.now(timezone.utc), job_id=running)
    _listing(client)
    _listing(client)
    lookups = [c.path for c in v_srv.calls("GET", "/video/jobs/")]
    assert lookups.count(f"/video/jobs/{failed}") == 1
    assert lookups.count(f"/video/jobs/{running}") == 2


def test_video_ingest_down_degrades_without_breaking_the_list(stack, monkeypatch):
    client, authority, ingest, a_srv, v_srv, _ = stack
    m = authority.add(created_at=datetime.now(timezone.utc) - timedelta(days=2), job_id=99)
    v_srv.stop()
    monkeypatch.setenv("VIDEO_INGEST_BASE_URL", "http://127.0.0.1:9")  # port fermé
    it = _listing(client)[m["id"]]
    assert it["materialization_status"] == "pending"
    assert it["job_status"] is None
    assert it["stale"] is True  # sans verdict, l'ancien signal reste
