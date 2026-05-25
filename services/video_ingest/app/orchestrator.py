"""Pipeline d'ingestion (cf. spec §4.4).

Reçoit un `Job` claimé par le worker, exécute :
  1. Routing provider via `matches_url`
  2. Parse canonical id → lookup dédup
  3. HIT cache : bookmark + complete avec `reused=true`
  4. MISS : fetch_metadata → upsert source
  5. Si !force_audio : tenter fetch_subtitles → chunking → insert transcript
  6. Si force_audio OU sous-titres absents : signaler `needs_audio` (fallback
     ASR sera implémenté en slice ASR ultérieure — V1 retourne `failed`
     pour l'instant avec un message explicite).
  7. Bookmark + complete avec `reused=false`.

L'orchestrateur ne tire aucun appel HTTP en direct : tout passe par
le provider (qui passe par yt-dlp/youtube-transcript-api → env proxy
honorée, cf. D15).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import jobs as jobs_mod
from . import repo
from .chunking import chunk
from .jobs import Job
from .providers.base import (
    ProviderError,
    SubtitlesUnavailable,
    VideoProvider,
    VideoUnavailable,
)


_DEFAULT_LANGUAGES = ["fr", "en"]


class NeedsAudioFallback(ProviderError):
    """Levée quand les sous-titres sont absents et que le fallback ASR
    serait nécessaire. V1 : signalé comme `failed`. Slice ASR : sera
    intercepté pour déclencher le pipeline Whisper."""


@dataclass(frozen=True)
class IngestResult:
    video_source_id: int
    reused: bool


def run_job(conn, providers: list[VideoProvider], job: Job) -> IngestResult:
    """Exécute le pipeline pour un job. Idempotent côté BDD (ON CONFLICT
    sur sources et transcripts), donc rejouer un job partiellement traité
    est sans risque.
    """
    provider = _select_provider(providers, job.url)

    try:
        provider_video_id, canonical_url = provider.parse_canonical_id(job.url)
    except ProviderError:
        raise

    # 2. Lookup dédup
    existing_id = repo.find_source_by_provider_id(
        conn, provider=provider.name, provider_video_id=provider_video_id,
    )

    if existing_id is not None and repo.has_transcript(
        conn, video_source_id=existing_id, language=job.language_pref,
    ):
        # 3. HIT cache parfait : source ET transcript exploitable existent.
        repo.add_bookmark(
            conn, user_sub=job.user_sub, video_source_id=existing_id,
            context=job.context, context_id=job.context_id,
        )
        return IngestResult(video_source_id=existing_id, reused=True)

    # 4. MISS (ou source sans transcript) : fetch metadata + upsert.
    metadata = provider.fetch_metadata(provider_video_id)
    video_source_id = repo.upsert_source(conn, metadata)

    # 5. Stratégie de récupération du transcript.
    languages = [job.language_pref] if job.language_pref else _DEFAULT_LANGUAGES
    transcript = None
    if not job.force_audio:
        try:
            transcript = provider.fetch_subtitles(provider_video_id, languages)
        except SubtitlesUnavailable:
            # Bascule automatique sur ASR — Principe 1 : sous-titres
            # prioritaires, mais on essaie quand même de servir l'usage
            # plutôt que de marquer failed.
            transcript = None

    if transcript is None:
        # Fallback ASR (chemin A : Kevent, cf. INTEGRATION_NOTES §2).
        # Peut lever NotImplementedError si l'env Kevent n'est pas configuré
        # ou si le provider n'a pas de fetch_audio — auquel cas on remonte
        # `NeedsAudioFallback` pour un retry manuel.
        try:
            transcript = provider.fetch_audio(provider_video_id, language=languages[0])
        except NotImplementedError as e:
            raise NeedsAudioFallback(str(e)) from e

    # 6. Chunking → persistance.
    segments_json = chunk(transcript.segments)
    repo.insert_transcript(
        conn, video_source_id=video_source_id,
        transcript=transcript,
        segments_json=segments_json,
        content_text=transcript.full_text,       # V1 = raw (post-traitement LLM = V1.5)
        content_text_raw=transcript.full_text,
    )

    # 7. Bookmark + retour.
    repo.add_bookmark(
        conn, user_sub=job.user_sub, video_source_id=video_source_id,
        context=job.context, context_id=job.context_id,
    )
    return IngestResult(video_source_id=video_source_id, reused=False)


def _select_provider(providers: list[VideoProvider], url: str) -> VideoProvider:
    for p in providers:
        if p.matches_url(url):
            return p
    raise ProviderError(f"Aucun provider ne reconnaît l'URL : {url!r}")


def run_and_record(conn, providers: list[VideoProvider], job: Job) -> None:
    """Variante intégrée file de jobs : exécute + écrit le résultat.

    Convention V1 :
      - succès → `complete(reused=…)`
      - `VideoUnavailable` ou `ProviderError` → `fail` (pas de retry auto)
      - `NeedsAudioFallback` → `fail` avec message explicite (sera intercepté
        en slice ASR pour requeue avec force_audio=True)

    Le worker appelle cette fonction, l'orchestrateur reste réutilisable
    sans la file (utile pour tests d'intégration et pour un mode CLI admin).
    """
    try:
        result = run_job(conn, providers, job)
    except VideoUnavailable as e:
        jobs_mod.fail(conn, job.id, error=f"video_unavailable: {e}")
    except NeedsAudioFallback as e:
        jobs_mod.fail(conn, job.id, error=f"needs_audio: {e}")
    except ProviderError as e:
        jobs_mod.fail(conn, job.id, error=f"provider_error: {e}")
    else:
        jobs_mod.complete(
            conn, job.id,
            video_source_id=result.video_source_id, reused=result.reused,
        )
