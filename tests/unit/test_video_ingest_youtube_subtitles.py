"""Tests du fetch sous-titres YouTube — mock complet de youtube-transcript-api.

Couvre :
- préférence manuels > auto
- bascule sur auto si pas de manuels dans les langues demandées
- SubtitlesUnavailable si rien ne matche
- VideoUnavailable propagé
- erreur listing → ProviderError
- payload de segments vide → SubtitlesUnavailable
"""

from unittest.mock import MagicMock, patch

import pytest
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
)
from youtube_transcript_api._errors import (
    VideoUnavailable as _YtaVideoUnavailable,
)

from services.video_ingest.app.providers.base import (
    ProviderError,
    SubtitlesUnavailable,
    VideoUnavailable,
)
from services.video_ingest.app.providers.youtube import subtitles as sub


def _snippet(text, start, duration):
    s = MagicMock()
    s.text = text
    s.start = start
    s.duration = duration
    return s


def _transcript(language_code, snippets):
    t = MagicMock()
    t.language_code = language_code
    t.fetch.return_value = snippets
    return t


def _api_with(list_obj):
    api = MagicMock()
    api.list.return_value = list_obj
    return MagicMock(return_value=api)


def _nf():
    """Construit une vraie NoTranscriptFound (signature stricte)."""
    return NoTranscriptFound("vid", ["fr"], MagicMock())


def test_manual_subtitles_preferred_over_auto():
    manual = _transcript("fr", [_snippet("Bonjour", 0.0, 2.5), _snippet("le monde", 2.5, 1.5)])
    tlist = MagicMock()
    tlist.find_manually_created_transcript.return_value = manual

    with patch.object(sub, "YouTubeTranscriptApi", _api_with(tlist)):
        result = sub.fetch("vid", ["fr", "en"])

    assert result.language == "fr"
    assert result.method == "subtitle_manual"
    assert len(result.segments) == 2
    assert result.segments[0].text == "Bonjour"
    assert result.segments[0].start_seconds == 0.0
    assert result.segments[0].duration_seconds == 2.5
    assert result.full_text == "Bonjour le monde"
    tlist.find_generated_transcript.assert_not_called()


def test_falls_back_to_auto_when_no_manual():
    auto = _transcript("fr", [_snippet("auto-text", 0.0, 3.0)])
    tlist = MagicMock()
    tlist.find_manually_created_transcript.side_effect = _nf()
    tlist.find_generated_transcript.return_value = auto

    with patch.object(sub, "YouTubeTranscriptApi", _api_with(tlist)):
        result = sub.fetch("vid", ["fr"])

    assert result.method == "subtitle_auto"
    assert result.language == "fr"


def test_no_transcript_at_all_raises_subtitles_unavailable():
    tlist = MagicMock()
    tlist.find_manually_created_transcript.side_effect = _nf()
    tlist.find_generated_transcript.side_effect = _nf()

    with patch.object(sub, "YouTubeTranscriptApi", _api_with(tlist)):
        with pytest.raises(SubtitlesUnavailable):
            sub.fetch("vid", ["fr"])


def test_transcripts_disabled_raises_subtitles_unavailable():
    api = MagicMock()
    api.list.side_effect = TranscriptsDisabled("vid")

    with patch.object(sub, "YouTubeTranscriptApi", MagicMock(return_value=api)):
        with pytest.raises(SubtitlesUnavailable):
            sub.fetch("vid", ["fr"])


def test_video_unavailable_propagates():
    api = MagicMock()
    api.list.side_effect = _YtaVideoUnavailable("vid")

    with patch.object(sub, "YouTubeTranscriptApi", MagicMock(return_value=api)):
        with pytest.raises(VideoUnavailable):
            sub.fetch("vid", ["fr"])


def test_other_listing_error_maps_to_provider_error():
    api = MagicMock()
    api.list.side_effect = RuntimeError("network exploded")

    with patch.object(sub, "YouTubeTranscriptApi", MagicMock(return_value=api)):
        with pytest.raises(ProviderError) as exc:
            sub.fetch("vid", ["fr"])
        assert not isinstance(exc.value, (VideoUnavailable, SubtitlesUnavailable))


def test_empty_segments_raises_subtitles_unavailable():
    empty = _transcript("fr", [])
    tlist = MagicMock()
    tlist.find_manually_created_transcript.return_value = empty

    with patch.object(sub, "YouTubeTranscriptApi", _api_with(tlist)):
        with pytest.raises(SubtitlesUnavailable):
            sub.fetch("vid", ["fr"])


def test_empty_languages_rejected_eagerly():
    with pytest.raises(ValueError):
        sub.fetch("vid", [])
