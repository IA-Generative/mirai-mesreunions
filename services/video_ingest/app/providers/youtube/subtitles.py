"""Fetch sous-titres YouTube via youtube-transcript-api.

Principe 1 : on essaie d'abord les manuels (qualité humaine), puis les
auto-générés en fallback. Plusieurs langues acceptées en entrée, on
prend la première qui rend un résultat.

La lib honore `HTTP_PROXY`/`HTTPS_PROXY` via `requests` (trust_env).
"""

from __future__ import annotations

from typing import Iterable

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable as _YtaVideoUnavailable,
)

from ...types import FetchedTranscript, TranscriptSegment
from ..base import SubtitlesUnavailable, VideoUnavailable
from . import _errors


def fetch(video_id: str, languages: Iterable[str]) -> FetchedTranscript:
    """Récupère les meilleurs sous-titres disponibles.

    Ordre de préférence :
      1. sous-titres MANUELS dans une des langues demandées (par ordre)
      2. sous-titres AUTO-GÉNÉRÉS dans une des langues demandées

    Lève :
      - `VideoUnavailable` si la vidéo est privée/retirée
      - `SubtitlesUnavailable` si aucun sous-titre exploitable
      - `ProviderError` pour les autres erreurs réseau/parsing
    """
    langs = list(languages)
    if not langs:
        raise ValueError("`languages` ne doit pas être vide")

    api = YouTubeTranscriptApi()
    try:
        transcript_list = api.list(video_id)
    except _YtaVideoUnavailable as e:
        raise VideoUnavailable(str(e)) from e
    except TranscriptsDisabled as e:
        raise SubtitlesUnavailable(f"Sous-titres désactivés sur la vidéo {video_id}") from e
    except Exception as e:  # noqa: BLE001 — la lib lève des erreurs hétérogènes
        # `IpBlocked` / `RequestBlocked` : l'IP d'egress est rate-limitée,
        # exactement le même phénomène que l'anti-bot yt-dlp. On classe
        # via le point unique pour que l'orchestrateur retente.
        raise _errors.classify(
            f"{type(e).__name__}: {e}", prefix=f"Échec listing transcripts {video_id}",
        ) from e

    # 1. Manuels d'abord.
    try:
        chosen = transcript_list.find_manually_created_transcript(langs)
        method = "subtitle_manual"
    except NoTranscriptFound:
        # 2. Fallback auto.
        try:
            chosen = transcript_list.find_generated_transcript(langs)
            method = "subtitle_auto"
        except NoTranscriptFound as e:
            raise SubtitlesUnavailable(
                f"Aucun sous-titre {langs} (ni manuel ni auto) sur {video_id}"
            ) from e

    fetched = chosen.fetch()
    segments = [
        TranscriptSegment(
            text=s.text,
            start_seconds=float(s.start),
            duration_seconds=float(s.duration),
        )
        for s in fetched
    ]
    if not segments:
        raise SubtitlesUnavailable(f"Sous-titres vides sur {video_id}")

    return FetchedTranscript(
        language=chosen.language_code,
        method=method,
        segments=segments,
    )
