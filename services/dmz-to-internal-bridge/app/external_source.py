"""Helpers pour la matérialisation d'imports externes (YouTube, MCR, DINUM…).

Ce module est délibérément **provider-agnostique** : il accepte des
segments au format canonique du contrat « Meeting Source Connector » et
produit les artefacts attendus par le pipeline meeting-intelligence
existant (cf. `puller._run_llm_chain_for_audio`).

Plan : ~/.claude/plans/l-importation-de-fichier-youtube-nifty-frost.md (C3)
"""

from __future__ import annotations

import json
import re
from typing import Iterable


def flatten_segments_to_synthetic_words(segments: Iterable[dict]) -> list[dict]:
    """Convertit des segments horodatés en mots horodatés synthétiques.

    Format d'entrée (canonique du contrat) :
        [{"start_seconds": float, "end_seconds": float, "text": str}, ...]

    Format de sortie (compatible Whisper word-level, cf.
    `user_audio_files.transcription_words_json`) :
        [{"w": str, "s": float, "e": float}, ...]

    Stratégie : chaque segment est tokenisé en mots (whitespace + ponctuation
    légère), puis on alloue à chaque mot une durée proportionnelle à sa
    longueur de caractères pour rester quasi-réaliste à la lecture
    karaoké (V2). Si un segment a 0 mots ou durée 0, on skip.

    Garanties :
      - words triés par `s` croissant
      - tous les `s <= e`
      - pas de chevauchement entre segments successifs (mais possible
        intra-segment si bornes du segment original sont incohérentes)
      - Unicode-safe (utf-8 dans le texte)
    """
    out: list[dict] = []
    for seg in segments or []:
        try:
            start = float(seg.get("start_seconds", 0.0))
            end = float(seg.get("end_seconds", 0.0))
        except (TypeError, ValueError):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        duration = max(0.0, end - start)
        # Tokenisation simple : on coupe sur whitespace mais on garde la
        # ponctuation collée au mot (suffisant pour la synchro karaoké).
        words = [w for w in re.split(r"\s+", text) if w]
        if not words:
            continue
        if duration <= 0:
            # Segment instantané (rare). On colle tous les mots à `start`.
            for w in words:
                out.append({"w": w, "s": start, "e": start})
            continue
        # Répartition proportionnelle à la longueur des mots.
        total_chars = sum(max(1, len(w)) for w in words)
        cursor = start
        for w in words:
            share = max(1, len(w)) / total_chars
            w_end = min(end, cursor + duration * share)
            out.append({"w": w, "s": round(cursor, 3), "e": round(w_end, 3)})
            cursor = w_end
    return out


def concat_segments_text(segments: Iterable[dict]) -> str:
    """Concatène le texte des segments avec un espace simple — base pour
    `transcription_text` du UAF virtuel."""
    parts = [(seg.get("text") or "").strip() for seg in (segments or [])]
    return " ".join(p for p in parts if p)


# ─── Logique métier de la route /api/v1/external-source/materialize ──────────
#
# Extraction de la logique non-Flask de la route pour permettre des tests
# unitaires en isolation totale (pas besoin de mocker tout l'écosystème
# d'imports du puller.py). La route Flask est un wrapper léger qui :
#   1. Vérifie l'auth (verify_token)
#   2. Valide le payload (validate_materialize_payload)
#   3. Appelle materialize_payload_to_uaf_kwargs pour construire les
#      champs du UAF virtuel
#   4. Persiste en DB + délègue au pipeline LLM en thread daemon

class MaterializeValidationError(ValueError):
    """Payload de /api/v1/external-source/materialize invalide."""


def derive_source_type(provider: str, method: str) -> str:
    """Détermine la valeur de `source_type` (migration 022) en fonction
    du provider et de la method.

    V1 : seul YouTube est géré finement (subtitle vs audio).
    V3+ introduira un type plus générique 'external_transcript' pour les
    autres providers (MCR, DINUM, ...).
    """
    provider = (provider or "").strip().lower()
    method = (method or "").strip().lower()
    if provider == "youtube":
        return "youtube_audio" if method.startswith("asr_") else "youtube_subtitle"
    # Fallback V1 : on tape sur 'youtube_subtitle' (= une valeur existante
    # de l'ENUM Postgres) pour ne pas casser. V3 étendra l'ENUM.
    return "youtube_subtitle"


def synthetic_session_code(provider: str, resource_id: str) -> str:
    """Génère un `original_session_code` valide (NOT NULL, max 10 chars)
    pour un UAF virtuel qui n'a pas de vrai code de session PWA."""
    rid_short = (resource_id or "extern")[:8]
    if (provider or "").strip().lower() == "youtube":
        return f"YT{rid_short}"[:10]
    return f"X{(provider or 'ext')[:9]}"[:10]


def validate_materialize_payload(data: dict) -> dict:
    """Valide + normalise le payload. Lève MaterializeValidationError sinon.

    Retourne un dict prêt à être passé à materialize_payload_to_uaf_kwargs.
    """
    if not isinstance(data, dict):
        raise MaterializeValidationError("payload must be a JSON object")
    provider = (data.get("provider") or "").strip()
    user_sub = (data.get("user_sub") or "").strip()
    if not provider:
        raise MaterializeValidationError("provider required")
    if not user_sub:
        raise MaterializeValidationError("user_sub required")
    segments = data.get("segments") or []
    transcript_text_explicit = data.get("transcript_text") or ""
    if not segments and not transcript_text_explicit:
        raise MaterializeValidationError("segments or transcript_text required")
    return {
        "provider": provider,
        "user_sub": user_sub,
        "segments": segments,
        "transcript_text_explicit": transcript_text_explicit,
        "method": (data.get("method") or "subtitle_auto").strip(),
        "language": (data.get("language") or "fr").strip(),
        "title": (data.get("title") or "").strip(),
        "duration_sec": data.get("duration_sec"),
        "external_video_source_id": data.get("external_video_source_id"),
        "meeting_id": (data.get("meeting_id") or "").strip() or None,
        "user_email": (data.get("user_email") or None),
        "source_resource_id": (data.get("source_resource_id") or "").strip(),
        "words_json": data.get("words_json"),
    }


def materialize_payload_to_uaf_kwargs(validated: dict) -> dict:
    """Construit les kwargs prêts à instancier un UserAudioFile.

    Délègue à `flatten_segments_to_synthetic_words` si `words_json` est
    absent, à `concat_segments_text` si `transcript_text` est absent.
    """
    transcript_text = (
        validated["transcript_text_explicit"]
        or concat_segments_text(validated["segments"])
    )
    words_json = (
        validated["words_json"]
        or flatten_segments_to_synthetic_words(validated["segments"])
    )
    duration = validated["duration_sec"]
    try:
        duration_float = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_float = None
    if duration_float is not None and duration_float <= 0:
        duration_float = None

    return {
        "user_sub": validated["user_sub"],
        "user_email": validated.get("user_email"),
        "original_session_code": synthetic_session_code(
            validated["provider"], validated["source_resource_id"]
        ),
        "original_filename": (validated["title"]
                              or f"{validated['provider']}:{validated['source_resource_id'][:8] or 'extern'}")[:512],
        "stored_filename": None,
        "file_size_bytes": len(transcript_text.encode("utf-8")),
        "audio_duration_seconds": duration_float,
        "origin": "upload",
        "source_type": derive_source_type(validated["provider"], validated["method"]),
        "external_video_source_id": (
            int(validated["external_video_source_id"])
            if validated["external_video_source_id"] is not None else None
        ),
        "transcription_status": "kevent_processing",
        "transcription_text": transcript_text,
        "transcription_language": validated["language"],
        # `transcription_words_json` est typé Text côté DB → sérialiser en
        # string JSON ici (sinon psycopg2 "can't adapt type dict").
        "transcription_words_json": json.dumps(words_json, ensure_ascii=False),
        "meeting_id": validated["meeting_id"],
    }
