"""Fetch métadonnées YouTube via yt-dlp (sans téléchargement).

yt-dlp honore nativement `HTTP_PROXY`/`HTTPS_PROXY` (mode B, cf. D15),
aucune config réseau custom ici.
"""

from __future__ import annotations

from datetime import datetime, timezone

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ...types import VideoMetadata
from ..base import ProviderError, VideoUnavailable

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": False,
    # Pas de cookies / pas de login : on ne traite que des vidéos publiques.
}


def fetch(video_id: str) -> VideoMetadata:
    """Renvoie les métadonnées d'une vidéo YouTube publique.

    Lève `VideoUnavailable` si vidéo privée/retirée/géo-bloquée,
    `ProviderError` pour toute autre erreur yt-dlp.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(_YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        msg = str(e).lower()
        if any(k in msg for k in ("private", "removed", "unavailable", "blocked")):
            raise VideoUnavailable(str(e)) from e
        raise ProviderError(str(e)) from e

    if not info:
        raise ProviderError(f"yt-dlp a renvoyé un payload vide pour {video_id}")

    return VideoMetadata(
        provider="youtube",
        provider_video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        duration_sec=int(info["duration"]) if info.get("duration") else None,
        published_at=_parse_upload_date(info.get("upload_date")),
        extra={
            "view_count": info.get("view_count"),
            "channel_id": info.get("channel_id"),
            "categories": info.get("categories"),
            "tags": info.get("tags"),
            "live_status": info.get("live_status"),
        },
    )


def _parse_upload_date(raw: str | None) -> datetime | None:
    """yt-dlp renvoie upload_date au format 'YYYYMMDD' (UTC implicite)."""
    if not raw or len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
