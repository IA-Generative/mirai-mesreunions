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

import logging
import os
from dataclasses import dataclass

import requests

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

log = logging.getLogger(__name__)


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
        # Hook materialize aussi sur HIT cache : sinon le user obtient
        # une row mais sans CR (pipeline LLM jamais déclenché). On
        # recharge le transcript depuis la BDD et on notifie.
        try:
            src = repo.load_source(conn, existing_id)
            tr = repo.load_best_transcript(conn, existing_id, language=job.language_pref)
            if src and tr:
                _notify_materialize_from_cache(
                    provider_name=provider.name,
                    provider_video_id=src["provider_video_id"],
                    video_source_id=existing_id,
                    job=job,
                    source_meta=src,
                    transcript_db=tr,
                )
        except Exception:
            log.exception("HIT cache materialize hook failed (non-fatal)")
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

    # 7bis. Hook materialize (C4) — best-effort, n'invalide pas le job.
    # Notifie internal-ingester pour qu'il crée un UAF virtuel et lance le
    # pipeline meeting-intelligence (glossary_correction → cleaning →
    # reformulation → meeting_analysis → suggested_filename → key_points).
    # Si MATERIALIZE_URL est vide, skip silencieux (mode standalone D14).
    _notify_materialize(
        provider_name=provider.name,
        provider_video_id=provider_video_id,
        video_source_id=video_source_id,
        job=job,
        metadata=metadata,
        transcript=transcript,
        segments_json=segments_json,
    )

    return IngestResult(video_source_id=video_source_id, reused=False)


def _notify_materialize(
    *,
    provider_name: str,
    provider_video_id: str,
    video_source_id: int,
    job: Job,
    metadata,
    transcript,
    segments_json: list,
) -> None:
    """Hook best-effort vers internal-ingester /api/v1/external-source/materialize.

    Configurable par env :
      - VIDEO_INGEST_MATERIALIZE_URL : URL complète de l'endpoint
        (ex. http://internal-ingester:8090/api/v1/external-source/materialize)
      - VIDEO_INGEST_INTERNAL_API_TOKEN : token Bearer partagé avec
        internal-ingester (INTERNAL_API_TOKEN du monorepo)
      - VIDEO_INGEST_MATERIALIZE_TIMEOUT : timeout secondes (défaut 10)

    Si MATERIALIZE_URL vide → skip propre (mode standalone D14 : video-ingest
    extrait dans son propre repo ne notifie personne).
    Si l'appel échoue → log warning, n'invalide PAS le job.
    """
    url = os.environ.get("VIDEO_INGEST_MATERIALIZE_URL", "").strip()
    if not url:
        log.debug("materialize hook skip: VIDEO_INGEST_MATERIALIZE_URL empty")
        return
    token = os.environ.get("VIDEO_INGEST_INTERNAL_API_TOKEN", "").strip()
    if not token:
        log.warning("materialize hook skip: VIDEO_INGEST_INTERNAL_API_TOKEN empty (set both URL+TOKEN)")
        return
    timeout = int(os.environ.get("VIDEO_INGEST_MATERIALIZE_TIMEOUT", "10"))

    # Mapping provider+transcript method → method canonique du contrat.
    method = transcript.method  # 'subtitle_manual' | 'subtitle_auto' | 'asr_whisper_kevent'
    if method.startswith("asr_"):
        method = "asr_whisper"

    payload = {
        "provider": provider_name,
        "source_resource_id": provider_video_id,
        "source_canonical_url": metadata.canonical_url,
        "user_sub": job.user_sub,
        "meeting_id": job.context_id if job.context == "meeting" else None,
        "title": metadata.title or "",
        "channel": metadata.channel or "",
        "duration_sec": metadata.duration_sec or 0,
        "language": transcript.language,
        "transcript_text": transcript.full_text,
        "segments": segments_json,
        "method": method,
        "external_video_source_id": video_source_id,
        # C5 — propagation du job_id pour que le Meeting placeholder soit
        # entièrement renseigné (meetings.video_ingest_job_id).
        "video_ingest_job_id": job.id,
    }
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.warning("materialize hook unreachable: %s — job continues, manual retry possible", e)
        return
    if resp.status_code >= 400:
        log.warning("materialize hook HTTP %d : %s", resp.status_code, resp.text[:200])
        return
    log.info(
        "materialize hook OK: vsid=%s user=%s meeting=%s",
        video_source_id, job.user_sub[:12], payload["meeting_id"],
    )


def notify_materialize_from_cache(
    *,
    provider_name: str,
    provider_video_id: str,
    video_source_id: int,
    user_sub: str,
    context: str | None,
    context_id: str | None,
    source_meta: dict,
    transcript_db: dict,
    job_id: int = 0,
) -> None:
    """Variante publique (sans préfixe `_`) pour usage depuis api.py.

    Permet de déclencher materialize en HIT cache synchrone (côté
    `POST /video/import` qui ne passe pas par le worker). Mêmes
    paramètres effectifs mais sans objet Job complet.
    """
    fake_job = Job(
        id=job_id, url=source_meta.get("canonical_url") or "",
        user_sub=user_sub, context=context, context_id=context_id,
        language_pref=transcript_db.get("language"),
        force_audio=False, attempts=0,
    )
    _notify_materialize_from_cache(
        provider_name=provider_name,
        provider_video_id=provider_video_id,
        video_source_id=video_source_id,
        job=fake_job,
        source_meta=source_meta,
        transcript_db=transcript_db,
    )


def _notify_materialize_from_cache(
    *,
    provider_name: str,
    provider_video_id: str,
    video_source_id: int,
    job: Job,
    source_meta: dict,
    transcript_db: dict,
) -> None:
    """Variante de _notify_materialize pour le chemin HIT cache.

    Construit le payload depuis les données déjà en BDD (pas de
    metadata/transcript fraîchement fetchés). Idempotent côté
    internal-ingester : si l'UAF existe déjà pour ce
    (user_sub, external_video_source_id), réutilise au lieu de créer.
    """
    url = os.environ.get("VIDEO_INGEST_MATERIALIZE_URL", "").strip()
    if not url:
        return
    token = os.environ.get("VIDEO_INGEST_INTERNAL_API_TOKEN", "").strip()
    if not token:
        return
    timeout = int(os.environ.get("VIDEO_INGEST_MATERIALIZE_TIMEOUT", "10"))

    method = transcript_db.get("method", "subtitle_auto")
    if method.startswith("asr_"):
        method = "asr_whisper"
    segments_json = transcript_db.get("segments_json") or []
    content_text = transcript_db.get("content_text") or ""

    payload = {
        "provider": provider_name,
        "source_resource_id": provider_video_id,
        "source_canonical_url": source_meta.get("canonical_url"),
        "user_sub": job.user_sub,
        "meeting_id": job.context_id if job.context == "meeting" else None,
        "title": source_meta.get("title") or "",
        "channel": source_meta.get("channel") or "",
        "duration_sec": source_meta.get("duration_sec") or 0,
        "language": transcript_db.get("language") or "fr",
        "transcript_text": content_text,
        "segments": segments_json,
        "method": method,
        "external_video_source_id": video_source_id,
        "video_ingest_job_id": job.id,
    }
    try:
        resp = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
    except requests.RequestException as e:
        log.warning("materialize-from-cache hook unreachable: %s", e)
        return
    if resp.status_code >= 400:
        log.warning("materialize-from-cache hook HTTP %d : %s",
                    resp.status_code, resp.text[:200])
        return
    log.info(
        "materialize-from-cache hook OK: vsid=%s user=%s meeting=%s",
        video_source_id, job.user_sub[:12], payload["meeting_id"],
    )


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
