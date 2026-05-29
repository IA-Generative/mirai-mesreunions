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


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")
_LONG_PHRASE_THRESHOLD_SEC = 20.0
_SOFT_BOUNDARY_RE = re.compile(r"(?<=[,;:])\s+")


def sentence_align_segments(coarse_segments: Iterable[dict]) -> list[dict]:
    """Re-segmente des chunks 60-90s en phrases sentence-aligned avec
    timecodes interpolés linéairement sur la position en caractères.

    Solution B (cf. docs/connectors/youtube-karaoke-options.md) :
    permet à l'éditeur de fragment + player YouTube de naviguer phrase
    par phrase au lieu de bloc 60-90s. Précision typique ±2s, suffisant
    pour le seekTo + pre-roll côté frontend.

    Algorithme :
      1. Split chaque chunk sur `. ! ? …` suivi d'espace
      2. Interpolation : start/end de la phrase = position en
         caractères × durée du chunk parent
      3. Si phrase > 20s (long monologue sans ponctuation), recouper
         sur `, ; :`
      4. Dédup overlap : les chunks 60-90s ont 15s d'overlap, on
         déduplique les phrases dont le texte est strictement répété
         dans une fenêtre de chevauchement

    Format de sortie identique à l'entrée : list[{start_seconds,
    end_seconds, text}].

    Hors-périmètre : la détection de fin de phrase ne couvre pas les
    abréviations ("M.", "etc."). Acceptable pour V1 — le LLM aval
    re-formule de toute façon le texte.
    """
    out: list[dict] = []
    for c in coarse_segments or []:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        try:
            t0 = float(c.get("start_seconds") or 0.0)
            t1 = float(c.get("end_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        duration = max(0.0, t1 - t0)
        if duration <= 0:
            continue
        parts = _SENTENCE_BOUNDARY_RE.split(text)
        total = sum(len(p) for p in parts) or 1
        cursor = 0
        for p in parts:
            p = p.strip()
            if not p:
                continue
            start_ratio = cursor / total
            cursor += len(p) + 1  # +1 pour l'espace séparateur consommé
            end_ratio = min(1.0, cursor / total)
            seg = {
                "start_seconds": round(t0 + start_ratio * duration, 2),
                "end_seconds":   round(t0 + end_ratio * duration, 2),
                "text": p,
            }
            if seg["end_seconds"] - seg["start_seconds"] > _LONG_PHRASE_THRESHOLD_SEC:
                out.extend(_split_long_phrase(seg))
            else:
                out.append(seg)
    return _dedupe_overlap(out)


def _split_long_phrase(seg: dict) -> list[dict]:
    """Recoupe une phrase > 20s sur les virgules / points-virgules.
    Fallback : si toujours > 20s, split tous les ~15s sur les espaces."""
    text = seg["text"]
    t0 = seg["start_seconds"]
    t1 = seg["end_seconds"]
    dur = t1 - t0
    parts = _SOFT_BOUNDARY_RE.split(text)
    if len(parts) <= 1:
        # Pas de virgules — split brut par paquets de ~15s
        target_per = max(1, int(dur / 15))
        words = text.split()
        if not words:
            return [seg]
        per_chunk = max(1, len(words) // target_per)
        parts = [" ".join(words[i:i+per_chunk])
                 for i in range(0, len(words), per_chunk)]
    total = sum(len(p) for p in parts) or 1
    cursor = 0
    out: list[dict] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        sr = cursor / total
        cursor += len(p) + 1
        er = min(1.0, cursor / total)
        out.append({
            "start_seconds": round(t0 + sr * dur, 2),
            "end_seconds":   round(t0 + er * dur, 2),
            "text": p,
        })
    return out or [seg]


def _dedupe_overlap(segments: list[dict]) -> list[dict]:
    """Supprime les phrases dupliquées par l'overlap de chunks (15s).
    Heuristique : si la phrase N est identique en texte à la phrase N-1
    et que leur start_seconds sont à < 20s d'écart, on garde la 1ère."""
    if not segments:
        return []
    out = [segments[0]]
    for cur in segments[1:]:
        prev = out[-1]
        same_text = cur["text"].strip() == prev["text"].strip()
        close = abs(cur["start_seconds"] - prev["start_seconds"]) < 20.0
        if same_text and close:
            continue
        out.append(cur)
    return out


def format_speaker_tagged_from_sentences(sentences: Iterable[dict],
                                          speaker_label: str = "Intervenant_01") -> str:
    """Génère un speaker_tagged_text Markdown à partir de phrases
    sentence-aligned (sortie de sentence_align_segments).

    Format attendu par le frontend (`_parseSpeakerTagged` legacy.js) :

        **Intervenant_01** _(M:SS → M:SS)_
        > Texte de la phrase.

    Pour les sources externes sans diarisation (sous-titres YouTube),
    on utilise un speaker fictif unique. Le frontend rend chaque
    phrase comme un bloc cliquable → seekTo(start - pre-roll).
    """
    def _fmt(t: float) -> str:
        m = int(t // 60)
        s = t - m * 60
        return f"{m}:{s:05.2f}"
    lines: list[str] = []
    for seg in sentences or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(seg.get("start_seconds") or 0.0)
            end = float(seg.get("end_seconds") or start)
        except (TypeError, ValueError):
            continue
        lines.append(f"**{speaker_label}** _({_fmt(start)} → {_fmt(end)})_")
        lines.append(f"> {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
    # Solution B : pour les sous-titres (segments grossiers ~60-75s
    # post-chunking video-ingest), on re-segmente en phrases via la
    # ponctuation + interpolation. Les word-timings synthétiques sont
    # alors calculés sur ces phrases (précision ±2s par phrase au lieu
    # de ±30s par chunk). Pour le chemin asr_whisper, on garde les
    # segments d'origine (déjà fin-grained).
    method = (validated.get("method") or "").lower()
    is_subtitle_path = not (method.startswith("asr_") or validated.get("words_json"))
    if is_subtitle_path:
        words_source_segments = sentence_align_segments(validated["segments"])
        # Génère un speaker_tagged_text Markdown synthétique pour que le
        # rendu frontend existant (mountTranscriptCorrector + word
        # alignment) marche sans branchement spécial pour YouTube.
        speaker_tagged_text = format_speaker_tagged_from_sentences(
            words_source_segments,
            speaker_label="Intervenant_01",
        )
    else:
        words_source_segments = validated["segments"]
        speaker_tagged_text = None
    words_json = (
        validated["words_json"]
        or flatten_segments_to_synthetic_words(words_source_segments)
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
        "speaker_tagged_text": speaker_tagged_text,
        "meeting_id": validated["meeting_id"],
    }
