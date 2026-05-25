"""Types de données échangés entre providers et reste du service.

Volontairement minimalistes (dataclasses immuables) — pas de SQLAlchemy
ici, l'objectif est qu'un provider puisse être testé sans BDD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class VideoMetadata:
    """Métadonnées d'une vidéo, ce qu'un provider sait dire sans transcoder.

    Mappe 1:1 sur les colonnes non-techniques de `video_sources` (cf.
    migration 019).
    """
    provider: str                       # "youtube" | "dailymotion" | ...
    provider_video_id: str              # ex. "dQw4w9WgXcQ"
    canonical_url: str                  # ex. "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    title: str | None = None
    channel: str | None = None
    duration_sec: int | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # → metadata_json


@dataclass(frozen=True)
class TranscriptSegment:
    """Un morceau temporel atomique tel que fourni par le provider.

    Pour YouTube subtitles : 1 segment = 1 ligne de sous-titre (durée
    typique 2-5s). Ces segments seront ensuite agrégés en chunks
    60-90s par `chunking.py` pour matcher le Principe 8.
    """
    text: str
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class FetchedTranscript:
    """Ce qu'un provider rapporte d'une fetch_subtitles() réussie.

    `method` colle aux valeurs de la colonne `video_transcripts.method` :
    `subtitle_manual` (sous-titres rédigés par l'auteur) ou
    `subtitle_auto` (auto-générés par YouTube).
    """
    language: str                       # ISO 639-1 : "fr", "en", ...
    method: str                         # "subtitle_manual" | "subtitle_auto" | "asr_*"
    segments: list[TranscriptSegment]

    @property
    def full_text(self) -> str:
        """Texte concaténé, séparateur espace (avant chunking)."""
        return " ".join(s.text.strip() for s in self.segments if s.text.strip())
