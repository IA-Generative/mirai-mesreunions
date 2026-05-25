"""Tests de cohésion du YouTubeProvider — façade au-dessus des modules
url/metadata/subtitles. Vérifie le contrat protocole + le routing.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.video_ingest.app.providers.base import ProviderError, VideoProvider
from services.video_ingest.app.providers.youtube import YouTubeProvider


def test_implements_protocol():
    assert isinstance(YouTubeProvider(), VideoProvider)


def test_name_is_youtube():
    assert YouTubeProvider().name == "youtube"


def test_matches_url_routing():
    p = YouTubeProvider()
    assert p.matches_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert p.matches_url("https://youtu.be/dQw4w9WgXcQ")
    assert not p.matches_url("https://vimeo.com/12345")


def test_parse_canonical_id_ok():
    p = YouTubeProvider()
    vid, canonical = p.parse_canonical_id("https://youtu.be/dQw4w9WgXcQ?t=42")
    assert vid == "dQw4w9WgXcQ"
    assert canonical == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_parse_canonical_id_invalid_raises_provider_error():
    p = YouTubeProvider()
    with pytest.raises(ProviderError):
        p.parse_canonical_id("https://vimeo.com/12345")


def test_fetch_metadata_delegates():
    p = YouTubeProvider()
    sentinel = MagicMock()
    with patch(
        "services.video_ingest.app.providers.youtube.metadata.fetch",
        return_value=sentinel,
    ) as m:
        result = p.fetch_metadata("dQw4w9WgXcQ")
    m.assert_called_once_with("dQw4w9WgXcQ")
    assert result is sentinel


def test_fetch_subtitles_delegates():
    p = YouTubeProvider()
    sentinel = MagicMock()
    with patch(
        "services.video_ingest.app.providers.youtube.subtitles.fetch",
        return_value=sentinel,
    ) as m:
        result = p.fetch_subtitles("dQw4w9WgXcQ", ["fr", "en"])
    m.assert_called_once_with("dQw4w9WgXcQ", ["fr", "en"])
    assert result is sentinel
