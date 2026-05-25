"""Provider YouTube : URL parsing + métadonnées + sous-titres.

Le fallback audio (`fetch_audio` → Whisper) viendra dans une slice
ultérieure, gated par le flag `force_audio` (cf. Principe 1).
"""

from __future__ import annotations

from typing import Iterable

from ...types import FetchedTranscript, VideoMetadata
from ..base import ProviderError
from . import metadata as _metadata
from . import subtitles as _subtitles
from . import url as _url


class YouTubeProvider:
    name = "youtube"

    def matches_url(self, url: str) -> bool:
        return _url.matches(url)

    def parse_canonical_id(self, url: str) -> tuple[str, str]:
        try:
            parsed = _url.parse(url)
        except _url.YouTubeUrlError as e:
            raise ProviderError(str(e)) from e
        return parsed.video_id, parsed.canonical_url

    def fetch_metadata(self, video_id: str) -> VideoMetadata:
        return _metadata.fetch(video_id)

    def fetch_subtitles(
        self, video_id: str, languages: Iterable[str]
    ) -> FetchedTranscript:
        return _subtitles.fetch(video_id, languages)

    def fetch_audio(self, video_id: str, language: str = "fr") -> FetchedTranscript:
        """Fallback ASR — non implémenté en V1, cf. INTEGRATION_NOTES.md §2.

        Lève `NotImplementedError` explicite : l'orchestrateur intercepte
        déjà ce cas via `NeedsAudioFallback` et le marque `failed` avec
        message « needs_audio: … ».
        """
        raise NotImplementedError(
            "fetch_audio (Whisper fallback) reste à implémenter — "
            "cf. services/video_ingest/INTEGRATION_NOTES.md §2"
        )


__all__ = ["YouTubeProvider"]
