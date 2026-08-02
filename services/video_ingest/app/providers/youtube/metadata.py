"""Fetch métadonnées YouTube via yt-dlp (sans téléchargement).

yt-dlp honore nativement `HTTP_PROXY`/`HTTPS_PROXY` (mode B, cf. D15),
aucune config réseau custom ici.

Deux chemins, dans cet ordre :

1. **yt-dlp** — complet (titre, chaîne, durée, date, tags, vues).
2. **oEmbed** — `https://www.youtube.com/oembed`, endpoint public
   documenté, *sans* contrôle anti-bot. Ne rend que titre + chaîne +
   miniature (pas de durée), mais suffit à ne pas bloquer un import de
   sous-titres derrière un mur anti-bot : `youtube-transcript-api` tape
   un tout autre endpoint et reste disponible.

Sans ce fallback, `fetch_metadata` était un portail bloquant : l'import
échouait en allant chercher un *titre*, alors que le transcript — le
seul contenu qui compte — était accessible. Kill-switch :
`VIDEO_INGEST_OEMBED_FALLBACK=0`.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from ...types import VideoMetadata
from ..base import ProviderError, TransientProviderError
from . import _errors

log = logging.getLogger(__name__)

_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": False,
    # Retries internes yt-dlp : absorbe le 429 isolé sans repasser par la
    # file de jobs. Le backoff long (orchestrator) prend le relais si le
    # blocage dure.
    "retries": 3,
    "extractor_retries": 3,
    # Pas de cookies / pas de login : on ne traite que des vidéos publiques.
}

_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
_OEMBED_TIMEOUT = int(os.environ.get("VIDEO_INGEST_OEMBED_TIMEOUT", "10"))


def fetch(video_id: str) -> VideoMetadata:
    """Renvoie les métadonnées d'une vidéo YouTube publique.

    Lève `VideoUnavailable` si vidéo privée/retirée/géo-bloquée,
    `TransientProviderError` si YouTube nous jette temporairement
    (anti-bot, 429) ET que le fallback oEmbed n'a rien donné non plus,
    `ProviderError` pour toute autre erreur yt-dlp.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(_YDL_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        err = _errors.classify(str(e))
        if isinstance(err, TransientProviderError) and _oembed_enabled():
            degraded = _fetch_oembed(video_id)
            if degraded is not None:
                log.warning(
                    "métadonnées dégradées via oEmbed pour %s (yt-dlp bloqué : %s)",
                    video_id, str(e)[:120],
                )
                return degraded
        raise err from e

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


def _oembed_enabled() -> bool:
    return os.environ.get("VIDEO_INGEST_OEMBED_FALLBACK", "1").lower() not in (
        "0", "false", "no",
    )


def _fetch_oembed(video_id: str) -> VideoMetadata | None:
    """Métadonnées minimales via l'endpoint oEmbed public.

    Renvoie `None` (et ne lève pas) si l'endpoint est lui aussi
    indisponible : le caller retombe alors sur l'erreur yt-dlp d'origine,
    qui reste la cause racine à signaler.

    `duration_sec` est laissé à `None` — oEmbed ne l'expose pas.
    L'orchestrateur le dérivera du dernier segment de sous-titres.
    """
    query = urllib.parse.urlencode({
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "format": "json",
    })
    try:
        with urllib.request.urlopen(
            f"{_OEMBED_ENDPOINT}?{query}", timeout=_OEMBED_TIMEOUT,
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        log.warning("fallback oEmbed indisponible pour %s : %s", video_id, e)
        return None

    title = payload.get("title")
    if not title:
        return None

    return VideoMetadata(
        provider="youtube",
        provider_video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        channel=payload.get("author_name"),
        duration_sec=None,
        published_at=None,
        extra={
            "metadata_source": "oembed",
            "metadata_degraded": True,
            "thumbnail_url": payload.get("thumbnail_url"),
            "author_url": payload.get("author_url"),
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
