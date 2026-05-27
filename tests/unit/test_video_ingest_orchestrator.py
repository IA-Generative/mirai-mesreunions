"""Tests du pipeline d'ingestion — cœur métier de video-ingest.

Tous les appels BDD et providers sont mockés. Couvre :
- HIT cache (source + transcript existants) → reused=True
- MISS → fetch metadata + sous-titres + chunking + persistance
- Source existante SANS transcript → on refetch (pas un vrai HIT)
- Sélection de provider (routing)
- Erreurs : VideoUnavailable, ProviderError, SubtitlesUnavailable → states finaux
- force_audio (V1) → NeedsAudioFallback
"""

from unittest.mock import MagicMock, patch

import pytest

from services.video_ingest.app import orchestrator
from services.video_ingest.app.jobs import Job
from services.video_ingest.app.providers.base import (
    ProviderError,
    SubtitlesUnavailable,
    VideoUnavailable,
)
from services.video_ingest.app.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)


def _job(**overrides):
    base = dict(
        id=42, url="https://youtu.be/dQw4w9WgXcQ", user_sub="user-123",
        context="meeting", context_id="meeting-99",
        language_pref=None, force_audio=False, attempts=1,
    )
    base.update(overrides)
    return Job(**base)


def _meta():
    return VideoMetadata(
        provider="youtube", provider_video_id="dQw4w9WgXcQ",
        canonical_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Test", channel="Chan", duration_sec=180,
    )


def _transcript():
    return FetchedTranscript(
        language="fr", method="subtitle_manual",
        segments=[TranscriptSegment("hello", 0.0, 2.0), TranscriptSegment("world", 2.0, 2.0)],
    )


def _provider(matches=True):
    p = MagicMock()
    p.name = "youtube"
    p.matches_url.return_value = matches
    p.parse_canonical_id.return_value = ("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    p.fetch_metadata.return_value = _meta()
    p.fetch_subtitles.return_value = _transcript()
    return p


# ─── HIT cache ─────────────────────────────────────────────────────────

def test_cache_hit_skips_fetch_and_marks_reused():
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=777), \
         patch.object(orchestrator.repo, "has_transcript", return_value=True), \
         patch.object(orchestrator.repo, "add_bookmark", return_value=999) as bk, \
         patch.object(orchestrator.repo, "upsert_source") as upsert, \
         patch.object(orchestrator.repo, "insert_transcript") as ins:

        result = orchestrator.run_job(conn, [provider], _job())

    assert result.reused is True
    assert result.video_source_id == 777
    provider.fetch_metadata.assert_not_called()
    provider.fetch_subtitles.assert_not_called()
    upsert.assert_not_called()
    ins.assert_not_called()
    bk.assert_called_once()


# ─── MISS / nominal ────────────────────────────────────────────────────

def test_cache_miss_runs_full_pipeline():
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript", return_value=33) as ins, \
         patch.object(orchestrator.repo, "add_bookmark") as bk:

        result = orchestrator.run_job(conn, [provider], _job())

    assert result.reused is False
    assert result.video_source_id == 555
    provider.fetch_metadata.assert_called_once_with("dQw4w9WgXcQ")
    provider.fetch_subtitles.assert_called_once_with("dQw4w9WgXcQ", ["fr", "en"])
    ins.assert_called_once()
    # Le caller doit avoir construit `segments_json` via chunking (liste non vide).
    kwargs = ins.call_args.kwargs
    assert isinstance(kwargs["segments_json"], list)
    assert len(kwargs["segments_json"]) >= 1
    assert kwargs["content_text"] == "hello world"
    assert kwargs["content_text_raw"] == "hello world"
    bk.assert_called_once()


def test_source_exists_but_no_transcript_triggers_refetch():
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=888), \
         patch.object(orchestrator.repo, "has_transcript", return_value=False), \
         patch.object(orchestrator.repo, "upsert_source", return_value=888), \
         patch.object(orchestrator.repo, "insert_transcript") as ins, \
         patch.object(orchestrator.repo, "add_bookmark"):

        result = orchestrator.run_job(conn, [provider], _job())

    assert result.reused is False  # pas un vrai HIT (transcript manquant)
    assert result.video_source_id == 888
    provider.fetch_metadata.assert_called_once()
    provider.fetch_subtitles.assert_called_once()
    ins.assert_called_once()


def test_language_pref_overrides_default_languages():
    conn = MagicMock()
    provider = _provider()
    job = _job(language_pref="en")

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=1), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"):

        orchestrator.run_job(conn, [provider], job)

    provider.fetch_subtitles.assert_called_once_with("dQw4w9WgXcQ", ["en"])


# ─── Routing provider ───────────────────────────────────────────────────

def test_provider_routing_picks_first_match():
    p1 = _provider(matches=False)
    p2 = _provider(matches=True)
    p3 = _provider(matches=True)
    p3.fetch_metadata.side_effect = AssertionError("p3 ne doit pas être appelé")

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=1), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"):

        orchestrator.run_job(MagicMock(), [p1, p2, p3], _job())

    p2.fetch_metadata.assert_called_once()


def test_no_provider_matches_raises_provider_error():
    p = _provider(matches=False)
    with pytest.raises(ProviderError):
        orchestrator.run_job(MagicMock(), [p], _job())


# ─── Erreurs métier ─────────────────────────────────────────────────────

def test_video_unavailable_propagates():
    conn = MagicMock()
    provider = _provider()
    provider.fetch_metadata.side_effect = VideoUnavailable("removed")

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None):
        with pytest.raises(VideoUnavailable):
            orchestrator.run_job(conn, [provider], _job())


def test_subtitles_unavailable_falls_back_to_audio():
    """Slice ASR : bascule auto sur fetch_audio quand sous-titres absents
    (Principe 1 — sous-titres prioritaires, mais on sert l'usage)."""
    conn = MagicMock()
    provider = _provider()
    provider.fetch_subtitles.side_effect = SubtitlesUnavailable("no captions")

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=1), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"):
        result = orchestrator.run_job(conn, [provider], _job())

    provider.fetch_audio.assert_called_once_with("dQw4w9WgXcQ", language="fr")
    assert result.reused is False


def test_force_audio_skips_subtitles_and_goes_to_audio():
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=1), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"):
        orchestrator.run_job(conn, [provider], _job(force_audio=True))

    provider.fetch_subtitles.assert_not_called()
    provider.fetch_audio.assert_called_once()


def test_fetch_audio_not_implemented_raises_needs_audio_fallback():
    """Provider sans support audio (ex. Dailymotion en V2 sans ASR) →
    on lève NeedsAudioFallback pour signaler que le job nécessite une
    config supplémentaire."""
    conn = MagicMock()
    provider = _provider()
    provider.fetch_subtitles.side_effect = SubtitlesUnavailable("no captions")
    provider.fetch_audio.side_effect = NotImplementedError("ASR pas configuré")

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=1):
        with pytest.raises(orchestrator.NeedsAudioFallback):
            orchestrator.run_job(conn, [provider], _job())


# ─── run_and_record : intégration file de jobs ─────────────────────────

def test_run_and_record_marks_complete_on_success():
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=777), \
         patch.object(orchestrator.repo, "has_transcript", return_value=True), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.jobs_mod, "complete") as comp, \
         patch.object(orchestrator.jobs_mod, "fail") as fail_mock:

        orchestrator.run_and_record(conn, [provider], _job())

    comp.assert_called_once()
    assert comp.call_args.kwargs["video_source_id"] == 777
    assert comp.call_args.kwargs["reused"] is True
    fail_mock.assert_not_called()


@pytest.mark.parametrize("exc_factory,expected_prefix", [
    (lambda: VideoUnavailable("private"),                              "video_unavailable:"),
    (lambda: orchestrator.NeedsAudioFallback("no caps"),               "needs_audio:"),
    (lambda: ProviderError("HTTP 500"),                                "provider_error:"),
])
def test_run_and_record_maps_errors_to_failed(exc_factory, expected_prefix):
    conn = MagicMock()
    provider = _provider()
    provider.fetch_metadata.side_effect = exc_factory()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.jobs_mod, "fail") as fail_mock, \
         patch.object(orchestrator.jobs_mod, "complete") as comp:

        orchestrator.run_and_record(conn, [provider], _job())

    comp.assert_not_called()
    fail_mock.assert_called_once()
    assert fail_mock.call_args.kwargs["error"].startswith(expected_prefix)


# ─── Hook materialize (C4 — plan video-ingest) ─────────────────────────

def test_materialize_skipped_when_url_env_empty(monkeypatch):
    """(i) MATERIALIZE_URL vide → skip propre, pas d'exception."""
    monkeypatch.delenv("VIDEO_INGEST_MATERIALIZE_URL", raising=False)
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post") as mock_post:
        result = orchestrator.run_job(conn, [provider], _job())

    assert result.video_source_id == 555
    mock_post.assert_not_called()  # URL vide → pas d'appel


def test_materialize_called_after_insert_transcript_when_configured(monkeypatch):
    """(g) Materialize appelé après insert_transcript."""
    monkeypatch.setenv("VIDEO_INGEST_MATERIALIZE_URL", "http://internal-ingester:8090/api/v1/external-source/materialize")
    monkeypatch.setenv("VIDEO_INGEST_INTERNAL_API_TOKEN", "fake-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    conn = MagicMock()
    provider = _provider()
    mock_resp = MagicMock(status_code=200)

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript") as ins, \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post", return_value=mock_resp) as mock_post:
        orchestrator.run_job(conn, [provider], _job())

    # materialize appelé après insert_transcript
    assert ins.called
    mock_post.assert_called_once()
    call = mock_post.call_args
    assert call.args[0].startswith("http://internal-ingester")
    assert call.kwargs["headers"]["Authorization"].startswith("Bearer fake-token")


def test_materialize_payload_contains_canonical_fields(monkeypatch):
    """(j) Payload contient provider, source_resource_id, user_sub, meeting_id,
    transcript_text, segments[], language."""
    monkeypatch.setenv("VIDEO_INGEST_MATERIALIZE_URL", "http://x/materialize")
    monkeypatch.setenv("VIDEO_INGEST_INTERNAL_API_TOKEN", "tok")
    conn = MagicMock()
    provider = _provider()
    mock_resp = MagicMock(status_code=200)

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post", return_value=mock_resp) as mock_post:
        orchestrator.run_job(conn, [provider], _job(context_id="meeting-uuid-42"))

    payload = mock_post.call_args.kwargs["json"]
    assert payload["provider"] == "youtube"
    assert payload["source_resource_id"] == "dQw4w9WgXcQ"
    assert payload["user_sub"] == "user-123"
    assert payload["meeting_id"] == "meeting-uuid-42"
    assert payload["transcript_text"] == "hello world"
    assert isinstance(payload["segments"], list)
    assert payload["language"] == "fr"
    assert payload["method"] == "subtitle_manual"
    assert payload["external_video_source_id"] == 555


def test_materialize_http_failure_does_not_invalidate_job(monkeypatch):
    """(h) Échec HTTP (502) loggé mais n'invalide PAS le job → result OK."""
    monkeypatch.setenv("VIDEO_INGEST_MATERIALIZE_URL", "http://x/materialize")
    monkeypatch.setenv("VIDEO_INGEST_INTERNAL_API_TOKEN", "tok")
    conn = MagicMock()
    provider = _provider()
    mock_resp = MagicMock(status_code=502, text="bad gateway")

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post", return_value=mock_resp):
        # NE doit PAS lever
        result = orchestrator.run_job(conn, [provider], _job())

    assert result.video_source_id == 555
    assert result.reused is False


def test_materialize_timeout_does_not_invalidate_job(monkeypatch):
    """Échec réseau (timeout/connection) loggé mais n'invalide pas le job."""
    monkeypatch.setenv("VIDEO_INGEST_MATERIALIZE_URL", "http://x/materialize")
    monkeypatch.setenv("VIDEO_INGEST_INTERNAL_API_TOKEN", "tok")
    conn = MagicMock()
    provider = _provider()

    import requests as _req
    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post", side_effect=_req.ConnectTimeout("boom")):
        result = orchestrator.run_job(conn, [provider], _job())

    assert result.video_source_id == 555  # job continue OK


def test_materialize_skipped_when_token_missing(monkeypatch):
    """URL configurée mais TOKEN vide → skip (config incomplete)."""
    monkeypatch.setenv("VIDEO_INGEST_MATERIALIZE_URL", "http://x/materialize")
    monkeypatch.delenv("VIDEO_INGEST_INTERNAL_API_TOKEN", raising=False)
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=None), \
         patch.object(orchestrator.repo, "upsert_source", return_value=555), \
         patch.object(orchestrator.repo, "insert_transcript"), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post") as mock_post:
        orchestrator.run_job(conn, [provider], _job())

    mock_post.assert_not_called()


def test_materialize_not_called_on_cache_hit(monkeypatch):
    """HIT cache → pas de transcript fraîchement inséré, donc pas de materialize."""
    monkeypatch.setenv("VIDEO_INGEST_MATERIALIZE_URL", "http://x/materialize")
    monkeypatch.setenv("VIDEO_INGEST_INTERNAL_API_TOKEN", "tok")
    conn = MagicMock()
    provider = _provider()

    with patch.object(orchestrator.repo, "find_source_by_provider_id", return_value=777), \
         patch.object(orchestrator.repo, "has_transcript", return_value=True), \
         patch.object(orchestrator.repo, "add_bookmark"), \
         patch.object(orchestrator.requests, "post") as mock_post:
        result = orchestrator.run_job(conn, [provider], _job())

    assert result.reused is True
    mock_post.assert_not_called()
