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

_WS_RE = re.compile(r"\s+")


def _normalize_ws(text: str | None) -> str:
    r"""Écrase tout blanc — espaces, tabulations et RETOURS À LA LIGNE — en un
    espace simple.

    Invariant indispensable au format `speaker_tagged_text` : une phrase doit
    tenir sur UNE ligne physique. Les cues de sous-titres YouTube sont très
    souvent sur deux lignes et `youtube-transcript-api` rend le `\n` tel quel.
    Ce `\n` traversait le chunking (`" ".join(s.text.strip())` ne touche que
    les bords) puis se retrouvait au milieu d'un bloc `> …`, produisant une
    ligne de continuation SANS `>` — que les deux parseurs (`_parseSpeakerTagged`
    côté legacy.js, `_reparse_speaker_tagged_blocks` côté puller.py) ignorent
    silencieusement. Mesuré en prod sur un import réel : 390 lignes orphelines,
    21 937 caractères, soit 54 % du transcript jamais affiché.
    """
    return _WS_RE.sub(" ", text or "").strip()


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
        text = _normalize_ws(seg.get("text"))
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
    # Le docstring promet un tri par `s` croissant : on le GARANTIT au lieu de
    # l'espérer. Le karaoké frontend fait une recherche dichotomique sur ce
    # tableau ; sur une entrée non triée elle rend un résultat arbitraire.
    out.sort(key=lambda w: (w["s"], w["e"]))
    return out


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")
_LONG_PHRASE_THRESHOLD_SEC = 20.0
_SOFT_BOUNDARY_RE = re.compile(r"(?<=[,;:])\s+")

# Fenêtre de rappel pour la dédup du recouvrement. Le chunking amont
# (`video_ingest.chunking.chunk`, overlap_seconds=15.0) fait apparaître les
# phrases de la zone de recouvrement dans DEUX chunks consécutifs, avec des
# timecodes interpolés différents de part et d'autre. On regarde en arrière un
# peu plus large que ce recouvrement pour les rattraper toutes.
_OVERLAP_LOOKBACK_SEC = 25.0

# Clé de comparaison : casse et ponctuation retirées. Les deux copies d'une
# phrase à cheval sur une frontière de chunk peuvent être tokenisées
# différemment (virgule finale absorbée d'un côté, pas de l'autre) — une
# égalité stricte de chaîne les manquerait.
_DEDUPE_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _dedupe_key(text: str) -> str:
    return _DEDUPE_STRIP_RE.sub("", _normalize_ws(text).lower())


# Longueur minimale d'une clé pour qu'une relation préfixe/suffixe soit tenue
# pour un fragment de la MÊME phrase. Sans ce garde-fou, une phrase courte
# (« Oui. », « Merci. ») serait absorbée par n'importe quelle phrase voisine
# qui se termine pareil.
_FRAGMENT_MIN_KEY_LEN = 25


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
        text = _normalize_ws(c.get("text"))
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
    return _enforce_monotonic(_dedupe_overlap(out))


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
    """Supprime les phrases dupliquées par le recouvrement des chunks (15 s).

    L'implémentation précédente ne comparait qu'au segment IMMÉDIATEMENT
    précédent. Or un recouvrement de 15 s porte typiquement 3 à 6 phrases : la
    séquence est `… S1 S2 S3 | S1 S2 S3 …` et aucune comparaison consécutive
    n'est une égalité — donc rien n'était jamais dédupliqué. Mesuré en prod sur
    un import réel : 58 blocs strictement identiques sur 428.

    On compare désormais à TOUTES les phrases déjà retenues dans la fenêtre de
    recouvrement, sur une clé normalisée (cf. `_dedupe_key`).
    """
    out: list[dict] = []
    recent: list[tuple[float, str, int]] = []   # (start, clé, index dans out)
    for cur in segments or []:
        key = _dedupe_key(cur.get("text", ""))
        if not key:
            continue
        start = float(cur["start_seconds"])
        # Purge ce qui est sorti de la fenêtre. Une copie issue du chunk
        # suivant démarre AVANT l'originale (écart négatif) : elle reste donc
        # bien dans la fenêtre, c'est exactement le cas qu'on veut attraper.
        recent = [r for r in recent if start - r[0] <= _OVERLAP_LOOKBACK_SEC]
        drop = False
        for pos, (s0, k0, idx) in enumerate(recent):
            if k0 == key:
                drop = True
                break
            # Une frontière de chunk tombe au MILIEU d'une phrase : le chunk
            # qui s'arrête n'en livre que le début, celui qui reprend la livre
            # entière (et livre en tête le reste de la phrase précédente).
            # Ces deux moitiés ne sont pas égales — seulement préfixe/suffixe
            # l'une de l'autre.
            if min(len(k0), len(key)) < _FRAGMENT_MIN_KEY_LEN:
                continue
            if key.startswith(k0) or key.endswith(k0):
                # Ce qui est déjà retenu est le FRAGMENT. On le complète sur
                # place au lieu d'ajouter un doublon : on garde son `start`
                # (celui du chunk où la phrase commence vraiment, donc le plus
                # fidèle) et on prend le texte complet du candidat.
                out[idx] = {**out[idx],
                            "text": cur["text"],
                            "end_seconds": max(float(out[idx]["end_seconds"]),
                                               float(cur["end_seconds"]))}
                recent[pos] = (s0, key, idx)
                drop = True
                break
            if k0.startswith(key) or k0.endswith(key):
                # Le candidat est le fragment : la phrase est déjà couverte.
                drop = True
                break
        if drop:
            continue
        out.append(cur)
        recent.append((start, key, len(out) - 1))
    return out


def _enforce_monotonic(segments: list[dict]) -> list[dict]:
    """Garantit une timeline non décroissante.

    Le karaoké frontend fait une recherche dichotomique sur les words dérivés
    de ces segments : elle exige un tableau trié par `s`. Après dédup il peut
    subsister des reculs — une phrase à cheval sur une frontière de chunk,
    tokenisée différemment de part et d'autre, échappe à la comparaison de clé.

    On RECALE ces segments plutôt que de les jeter : perdre du texte pour
    sauver la monotonie remplacerait un bug par un autre. Le recalage est borné
    (`end` ne dépasse jamais sa valeur d'origine), donc pas de dérive cumulée
    sur la suite de la timeline.
    """
    out: list[dict] = []
    prev_end = 0.0
    for seg in segments or []:
        start = max(float(seg["start_seconds"]), prev_end)
        end = max(float(seg["end_seconds"]), start)
        out.append({**seg,
                    "start_seconds": round(start, 2),
                    "end_seconds": round(end, 2)})
        prev_end = end
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
        # `_normalize_ws` est la GARANTIE que `> {text}` tient sur une seule
        # ligne physique — sans elle, un `\n` résiduel casse le parsing aval.
        text = _normalize_ws(seg.get("text"))
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
    parts = [_normalize_ws(seg.get("text")) for seg in (segments or [])]
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
