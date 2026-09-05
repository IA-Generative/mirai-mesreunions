"""Tests de la logique métier de l'endpoint /api/v1/external-source/materialize.

Les helpers métier (validate_materialize_payload, materialize_payload_to_uaf_kwargs,
derive_source_type, synthetic_session_code) sont extraits du puller.py en
fonctions pures testables (cf. external_source.py). La route Flask elle-même
est un wrapper léger : auth + validate + write DB + thread daemon.

Couvre (cf. plan C3) :
- (n) auth via verify_token (testé indirectement — sans token = 401, mais ici
      on teste les helpers en isolation)
- (o) payload invalide → MaterializeValidationError
- (p) path nominal : génère les bons kwargs UAF
- (q) idempotence : couvert par le test de l'endpoint Flask en intégration smoke
"""

import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_SPEC = importlib.util.spec_from_file_location(
    "dmz_to_internal_bridge_external_source",
    os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "external_source.py"),
)
_es = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_es)

import pytest


# ── derive_source_type ──────────────────────────────────────────────────

def test_derive_source_type_youtube_subtitle():
    assert _es.derive_source_type("youtube", "subtitle_auto") == "youtube_subtitle"
    assert _es.derive_source_type("youtube", "subtitle_manual") == "youtube_subtitle"


def test_derive_source_type_youtube_audio():
    assert _es.derive_source_type("youtube", "asr_whisper") == "youtube_audio"
    assert _es.derive_source_type("youtube", "asr_whisper_v3") == "youtube_audio"


def test_derive_source_type_other_provider_falls_back():
    # V1 : tout provider non-youtube → 'youtube_subtitle' (ENUM existant)
    assert _es.derive_source_type("mcr", "external_transcript") == "youtube_subtitle"
    assert _es.derive_source_type("dictaphone-dinum", "subtitle_auto") == "youtube_subtitle"


# ── synthetic_session_code ──────────────────────────────────────────────

def test_synthetic_session_code_youtube():
    code = _es.synthetic_session_code("youtube", "dQw4w9WgXcQ")
    assert code.startswith("YT")
    assert len(code) <= 10


def test_synthetic_session_code_other():
    code = _es.synthetic_session_code("mcr", "abc123")
    assert code.startswith("X")
    assert len(code) <= 10


def test_synthetic_session_code_empty_resource_id():
    code = _es.synthetic_session_code("youtube", "")
    assert code == "YTextern"  # fallback "extern"[:8]
    assert len(code) <= 10


# ── validate_materialize_payload ────────────────────────────────────────

def test_validate_rejects_non_dict():
    with pytest.raises(_es.MaterializeValidationError):
        _es.validate_materialize_payload(None)
    with pytest.raises(_es.MaterializeValidationError):
        _es.validate_materialize_payload("string")


def test_validate_rejects_missing_provider():
    with pytest.raises(_es.MaterializeValidationError, match="provider"):
        _es.validate_materialize_payload({"user_sub": "u-1", "segments": [{}]})


def test_validate_rejects_missing_user_sub():
    with pytest.raises(_es.MaterializeValidationError, match="user_sub"):
        _es.validate_materialize_payload({"provider": "youtube", "segments": [{}]})


def test_validate_rejects_no_segments_no_transcript():
    with pytest.raises(_es.MaterializeValidationError, match="transcript"):
        _es.validate_materialize_payload({"provider": "youtube", "user_sub": "u"})


def test_validate_accepts_with_segments():
    v = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "segments": [{"start_seconds": 0, "end_seconds": 1, "text": "hi"}],
    })
    assert v["provider"] == "youtube"
    assert v["language"] == "fr"  # default
    assert v["method"] == "subtitle_auto"  # default


def test_validate_accepts_with_transcript_text():
    v = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "transcript_text": "Bonjour le monde",
        "language": "en",
        "duration_sec": 60,
    })
    assert v["language"] == "en"
    assert v["transcript_text_explicit"] == "Bonjour le monde"


# ── materialize_payload_to_uaf_kwargs ───────────────────────────────────

def test_uaf_kwargs_nominal():
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "segments": [{"start_seconds": 0.0, "end_seconds": 4.0, "text": "Bonjour le monde"}],
        "title": "Test Vidéo",
        "duration_sec": 245,
        "language": "fr",
        "method": "subtitle_manual",
        "external_video_source_id": 42,
        "source_resource_id": "dQw4w9WgXcQ",
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert kw["user_sub"] == "u-1"
    assert kw["source_type"] == "youtube_subtitle"
    assert kw["external_video_source_id"] == 42
    assert kw["transcription_status"] == "kevent_processing"
    assert kw["transcription_text"] == "Bonjour le monde"
    assert kw["transcription_language"] == "fr"
    assert kw["stored_filename"] is None  # nullable depuis 022
    assert kw["origin"] == "upload"
    assert kw["original_session_code"].startswith("YT")
    assert kw["original_filename"] == "Test Vidéo"
    assert kw["audio_duration_seconds"] == 245.0
    # words synthétisés depuis les segments → sérialisés en string JSON
    # (transcription_words_json est typé Text côté DB).
    import json as _json
    words_decoded = _json.loads(kw["transcription_words_json"])
    assert len(words_decoded) == 3  # "Bonjour", "le", "monde"
    # file_size = bytes UTF-8 du transcript
    assert kw["file_size_bytes"] == len("Bonjour le monde".encode("utf-8"))


def test_uaf_kwargs_force_audio_maps_to_youtube_audio():
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "transcript_text": "Whisper output",
        "method": "asr_whisper",
        "external_video_source_id": 99,
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert kw["source_type"] == "youtube_audio"


def test_uaf_kwargs_uses_explicit_transcript_over_segments():
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "transcript_text": "Explicite",
        "segments": [{"start_seconds": 0, "end_seconds": 1, "text": "implicite"}],
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert kw["transcription_text"] == "Explicite"


def test_uaf_kwargs_uses_explicit_words_over_synthesized():
    explicit_words = [{"w": "Manual", "s": 0.0, "e": 1.0}]
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "transcript_text": "Manual",
        "words_json": explicit_words,
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    # Sérialisé en string JSON pour insertion DB
    import json as _json
    assert _json.loads(kw["transcription_words_json"]) == explicit_words


def test_uaf_kwargs_duration_zero_becomes_none():
    """duration_sec = 0 ne doit pas créer audio_duration_seconds = 0
    (gardé None pour éviter divisions par zéro en aval)."""
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "transcript_text": "x",
        "duration_sec": 0,
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert kw["audio_duration_seconds"] is None


def test_uaf_kwargs_meeting_id_propagated():
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "transcript_text": "x",
        "meeting_id": "abc-def-uuid",
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert kw["meeting_id"] == "abc-def-uuid"


def test_uaf_kwargs_no_external_video_source_id():
    """Cas non-YouTube : external_video_source_id peut être absent."""
    validated = _es.validate_materialize_payload({
        "provider": "mcr",
        "user_sub": "u-1",
        "transcript_text": "x",
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert kw["external_video_source_id"] is None


# ── Non-régression : alignement karaoké des imports sous-titres ─────────
#
# Trois bugs mesurés en prod sur un import YouTube réel (fiche
# 88bb47de-06a6-49d5-b444-6569ed38079d) :
#   - 390 lignes orphelines / 21 937 caractères jamais affichés, parce que le
#     `\n` interne des cues survivait jusqu'au Markdown `> …` ;
#   - 58 blocs strictement dupliqués, parce que la dédup ne comparait qu'au
#     segment immédiatement précédent alors qu'un recouvrement de 15 s porte
#     plusieurs phrases ;
#   - 29 sauts en arrière dans la timeline, qui invalident la recherche
#     dichotomique du karaoké frontend (elle exige un tableau trié).

import json as _json
import re as _re

_HEAD_RE = _re.compile(
    r"^\*\*([^*]+)\*\*\s*_\((\d+):(\d+(?:\.\d+)?)\s*→\s*(\d+):(\d+(?:\.\d+)?)\)_"
)


def _orphan_lines(speaker_tagged: str) -> list:
    """Lignes qu'aucun des deux parseurs ne sait rattacher à un bloc."""
    return [ln for ln in speaker_tagged.split("\n")
            if ln.strip() and not ln.startswith(">") and not _HEAD_RE.match(ln)]


def test_speaker_tagged_never_leaks_a_newline_into_a_quote_block():
    """Un cue sur deux lignes ne doit pas produire de ligne sans « > »."""
    segments = [
        {"start_seconds": 0.0, "end_seconds": 6.0,
         "text": "Le marché de l'emploi a cessé de savoir \nnommer ce qu'il achète."},
        {"start_seconds": 6.0, "end_seconds": 10.0,
         "text": "Le titre n'est plus\nqu'un paravent."},
    ]
    sentences = _es.sentence_align_segments(segments)
    st = _es.format_speaker_tagged_from_sentences(sentences)

    assert _orphan_lines(st) == []
    # Et le texte perdu est bien de retour, en entier.
    assert "nommer ce qu'il achète." in st
    assert "qu'un paravent." in st


def test_speaker_tagged_survives_a_raw_newline_even_without_sentence_split():
    """Garantie d'invariant au niveau du formateur lui-même."""
    st = _es.format_speaker_tagged_from_sentences(
        [{"start_seconds": 0.0, "end_seconds": 3.0, "text": "deux\nlignes\tcollées"}]
    )
    assert _orphan_lines(st) == []
    assert "> deux lignes collées" in st


# Densité de parole, en caractères par seconde. `sentence_align_segments`
# interpole les timecodes sur la position en CARACTÈRES : une fixture dont la
# densité varie d'un chunk à l'autre produirait des timecodes incohérents et ne
# reproduirait pas le recouvrement réel.
_CHARS_PER_SEC = 5.0

_STRADDLING = "Une phrase coupée net par la frontière du chunk."
_SENTENCES = [
    "Phrase initiale du premier chunk.",
    "Deuxième phrase d'introduction du premier chunk.",
    "Troisième phrase d'introduction du premier chunk.",
    "Première phrase du recouvrement.",
    "Deuxième phrase du recouvrement.",
    "Troisième phrase du recouvrement.",
    _STRADDLING,
    "Phrase finale du second chunk.",
]


def _overlapping_chunks() -> list:
    """Deux chunks reproduisant le recouvrement de 15 s de `chunking.chunk`.

    Le second reprend les trois dernières phrases du premier. La phrase qui
    enjambe la frontière n'est livrée ENTIÈRE que par le second : le premier
    s'arrête au milieu — c'est exactement ce que fait `chunk()`, et c'est le
    cas qu'une dédup par égalité de texte ne peut pas voir.
    """
    head = " ".join(_SENTENCES[:3])
    overlap_and_rest = " ".join(_SENTENCES[3:])
    chunk1_text = head + " " + " ".join(_SENTENCES[3:6]) + " Une phrase coupée net par la"
    chunk2_start = (len(head) + 1) / _CHARS_PER_SEC
    return [
        {"start_seconds": 0.0,
         "end_seconds": len(chunk1_text) / _CHARS_PER_SEC,
         "text": chunk1_text},
        {"start_seconds": chunk2_start,
         "end_seconds": chunk2_start + len(overlap_and_rest) / _CHARS_PER_SEC,
         "text": overlap_and_rest},
    ]


def test_sentence_align_deduplicates_a_multi_sentence_overlap():
    sentences = _es.sentence_align_segments(_overlapping_chunks())
    texts = [s["text"] for s in sentences]
    for phrase in ("Première phrase du recouvrement.",
                   "Deuxième phrase du recouvrement.",
                   "Troisième phrase du recouvrement."):
        assert texts.count(phrase) == 1, f"{phrase!r} dupliqué : {texts}"


def test_sentence_align_keeps_the_complete_half_of_a_straddling_sentence():
    """La phrase coupée par la frontière survit ENTIÈRE, une seule fois."""
    sentences = _es.sentence_align_segments(_overlapping_chunks())
    full = [s for s in sentences if "coupée net par la" in s["text"]]
    assert len(full) == 1, [s["text"] for s in full]
    assert full[0]["text"] == "Une phrase coupée net par la frontière du chunk."


def test_sentence_align_output_is_monotonic():
    sentences = _es.sentence_align_segments(_overlapping_chunks())
    starts = [s["start_seconds"] for s in sentences]
    assert starts == sorted(starts), starts
    for a, b in zip(sentences, sentences[1:]):
        assert b["start_seconds"] >= a["end_seconds"] - 1e-9, (a, b)


def test_sentence_align_does_not_drop_text_it_cannot_dedupe():
    """La dédup ne doit jamais servir de prétexte à perdre du contenu."""
    sentences = _es.sentence_align_segments(_overlapping_chunks())
    joined = " ".join(s["text"] for s in sentences)
    for phrase in ("Phrase initiale du premier chunk.",
                   "Phrase finale du second chunk."):
        assert phrase in joined


def test_words_json_is_sorted_even_when_segments_are_not():
    """Le binary search karaoké exige un tableau trié — on le garantit ici."""
    words = _es.flatten_segments_to_synthetic_words([
        {"start_seconds": 30.0, "end_seconds": 34.0, "text": "arrivé en second"},
        {"start_seconds": 10.0, "end_seconds": 14.0, "text": "arrivé en premier"},
    ])
    starts = [w["s"] for w in words]
    assert starts == sorted(starts), starts
    assert all(w["s"] <= w["e"] for w in words)


def test_uaf_kwargs_end_to_end_on_overlapping_subtitle_chunks():
    """Le chemin complet materialize : ni texte perdu, ni doublon, ni recul."""
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "method": "subtitle_manual",
        "segments": _overlapping_chunks(),
        "external_video_source_id": 7,
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)

    assert _orphan_lines(kw["speaker_tagged_text"]) == []
    quoted = [ln for ln in kw["speaker_tagged_text"].split("\n") if ln.startswith(">")]
    assert len(quoted) == len(set(quoted)), "blocs dupliqués par le recouvrement"

    words = _json.loads(kw["transcription_words_json"])
    starts = [w["s"] for w in words]
    assert starts == sorted(starts), "timeline non monotone : karaoké cassé"


def test_uaf_kwargs_newline_in_segments_does_not_reach_transcription_text():
    validated = _es.validate_materialize_payload({
        "provider": "youtube",
        "user_sub": "u-1",
        "method": "subtitle_auto",
        "segments": [{"start_seconds": 0.0, "end_seconds": 4.0,
                      "text": "une phrase\ncoupée en deux"}],
    })
    kw = _es.materialize_payload_to_uaf_kwargs(validated)
    assert "\n" not in kw["transcription_text"]
    assert kw["transcription_text"] == "une phrase coupée en deux"
