"""Tests du fetch metadata YouTube — mock complet de yt-dlp, zéro réseau."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.video_ingest.app.providers.base import ProviderError, VideoUnavailable
from services.video_ingest.app.providers.youtube import metadata as md


def _ydl_returning(info: dict | None):
    """Construit un mock contextmanager YoutubeDL → extract_info(info)."""
    ydl = MagicMock()
    ydl.extract_info.return_value = info
    cm = MagicMock()
    cm.__enter__.return_value = ydl
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def test_fetch_basic_metadata():
    info = {
        "title": "Test Video",
        "channel": "Test Channel",
        "channel_id": "UC123",
        "uploader": "Test Uploader",
        "duration": 245.0,
        "upload_date": "20260520",
        "view_count": 1234,
        "categories": ["Education"],
        "tags": ["python", "test"],
        "live_status": "not_live",
    }
    with patch.object(md, "YoutubeDL", _ydl_returning(info)):
        result = md.fetch("dQw4w9WgXcQ")

    assert result.provider == "youtube"
    assert result.provider_video_id == "dQw4w9WgXcQ"
    assert result.canonical_url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert result.title == "Test Video"
    assert result.channel == "Test Channel"
    assert result.duration_sec == 245
    assert result.published_at == datetime(2026, 5, 20, tzinfo=timezone.utc)
    assert result.extra["view_count"] == 1234
    assert result.extra["channel_id"] == "UC123"


def test_fetch_falls_back_to_uploader_if_no_channel():
    info = {"title": "x", "uploader": "Solo Creator", "duration": 10}
    with patch.object(md, "YoutubeDL", _ydl_returning(info)):
        result = md.fetch("aaaaaaaaaaa")
    assert result.channel == "Solo Creator"


def test_fetch_missing_duration_is_none():
    info = {"title": "x", "channel": "y"}
    with patch.object(md, "YoutubeDL", _ydl_returning(info)):
        result = md.fetch("aaaaaaaaaaa")
    assert result.duration_sec is None
    assert result.published_at is None


def test_fetch_invalid_upload_date_is_none():
    info = {"title": "x", "upload_date": "garbage"}
    with patch.object(md, "YoutubeDL", _ydl_returning(info)):
        result = md.fetch("aaaaaaaaaaa")
    assert result.published_at is None


def test_fetch_empty_payload_raises_provider_error():
    with patch.object(md, "YoutubeDL", _ydl_returning(None)):
        with pytest.raises(ProviderError):
            md.fetch("aaaaaaaaaaa")


@pytest.mark.parametrize("err_msg", [
    "Video is private",
    "Video has been removed",
    "This video is unavailable",
    "Video blocked in your country",
])
def test_fetch_unavailable_messages_map_to_video_unavailable(err_msg):
    from yt_dlp.utils import DownloadError

    def raising(*a, **kw):
        raise DownloadError(err_msg)

    ydl = MagicMock()
    ydl.extract_info.side_effect = raising
    cm = MagicMock(); cm.__enter__.return_value = ydl; cm.__exit__.return_value = False

    with patch.object(md, "YoutubeDL", MagicMock(return_value=cm)):
        with pytest.raises(VideoUnavailable):
            md.fetch("aaaaaaaaaaa")


def test_fetch_other_download_error_maps_to_provider_error():
    from yt_dlp.utils import DownloadError

    def raising(*a, **kw):
        raise DownloadError("HTTP Error 429: Too Many Requests")

    ydl = MagicMock()
    ydl.extract_info.side_effect = raising
    cm = MagicMock(); cm.__enter__.return_value = ydl; cm.__exit__.return_value = False

    with patch.object(md, "YoutubeDL", MagicMock(return_value=cm)):
        with pytest.raises(ProviderError) as exc:
            md.fetch("aaaaaaaaaaa")
        assert not isinstance(exc.value, VideoUnavailable)
