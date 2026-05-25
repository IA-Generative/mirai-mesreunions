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
        """Fallback ASR via Kevent (cf. INTEGRATION_NOTES.md §2 chemin A).

        Télécharge l'audio dans un TemporaryDirectory (fichier supprimé
        immédiatement après transcription — DoD §10).
        """
        from . import audio as _audio
        return _audio.fetch_audio_and_transcribe(video_id, language=language)


__all__ = ["YouTubeProvider"]
