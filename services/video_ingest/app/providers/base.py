"""Interface contractuelle d'un VideoProvider (multi-source).

Un provider encapsule TOUT ce qui est spécifique à une plateforme
(URL parsing, métadonnées, sous-titres, audio fallback) derrière une
API stable consommée par le worker et la couche REST.

V1 : un seul provider implémenté (YouTube). V2 : Dailymotion suivra
sans toucher au worker.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from ..types import FetchedTranscript, VideoMetadata


class ProviderError(Exception):
    """Erreur métier provider (vidéo introuvable, sous-titres absents, etc.)."""


class VideoUnavailable(ProviderError):
    """Vidéo retirée, privée, géo-bloquée — pas de retry utile."""


class SubtitlesUnavailable(ProviderError):
    """Aucun sous-titre disponible dans les langues demandées. Le worker
    devra basculer sur le fallback ASR si `force_audio` ou si retry."""


@runtime_checkable
class VideoProvider(Protocol):
    """Toute classe respectant ce protocole peut être enregistrée."""

    name: str  # "youtube" | "dailymotion" | ...

    def matches_url(self, url: str) -> bool:
        """L'URL relève-t-elle de ce provider ? (routing)"""
        ...

    def parse_canonical_id(self, url: str) -> tuple[str, str]:
        """Renvoie `(provider_video_id, canonical_url)` à partir d'une URL
        brute. Lève `ProviderError` si l'URL est invalide."""
        ...

    def fetch_metadata(self, video_id: str) -> VideoMetadata:
        """Récupère les métadonnées (titre, chaîne, durée, date)."""
        ...

    def fetch_subtitles(
        self, video_id: str, languages: Iterable[str]
    ) -> FetchedTranscript:
        """Récupère les meilleurs sous-titres dispos dans l'une des
        langues demandées. Manuels prioritaires sur auto-générés.

        Lève `SubtitlesUnavailable` si aucune langue ne matche."""
        ...
