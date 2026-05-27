"""Tests des helpers external_source (C3 — plan video-ingest)."""

import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Charge external_source.py via spec (le module vit dans
# services/dmz-to-internal-bridge/app/ qui n'est pas un package importable
# par chemin dotted standard à cause du dash dans le nom du service).
_SPEC = importlib.util.spec_from_file_location(
    "dmz_to_internal_bridge_external_source",
    os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "external_source.py"),
)
_external_source = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_external_source)

flatten = _external_source.flatten_segments_to_synthetic_words
concat = _external_source.concat_segments_text


# ── flatten_segments_to_synthetic_words ────────────────────────────────

def test_empty_segments_returns_empty():
    """(a) format words synthétiques sur input vide."""
    assert flatten([]) == []
    assert flatten(None) == []  # type: ignore[arg-type]


def test_nominal_two_segments_words_equireparties():
    """(a) start <= end, équirépartis, ordre croissant."""
    segments = [
        {"start_seconds": 0.0, "end_seconds": 4.0, "text": "Bonjour le monde"},
        {"start_seconds": 4.0, "end_seconds": 8.0, "text": "Comment ça va"},
    ]
    words = flatten(segments)
    assert len(words) == 6
    # Triés par s croissant
    starts = [w["s"] for w in words]
    assert starts == sorted(starts)
    # Tous s <= e
    for w in words:
        assert w["s"] <= w["e"]
    # Premier mot commence à 0, dernier ≤ 8
    assert words[0]["s"] == 0.0
    assert words[-1]["e"] <= 8.0
    # Texte préservé
    assert [w["w"] for w in words] == ["Bonjour", "le", "monde", "Comment", "ça", "va"]


def test_segment_with_empty_text_skipped():
    """(b) segments vides → words=[]"""
    segments = [
        {"start_seconds": 0, "end_seconds": 1, "text": ""},
        {"start_seconds": 1, "end_seconds": 2, "text": "  "},
    ]
    assert flatten(segments) == []


def test_segment_instantane_zero_duration():
    """Segment à durée 0 : tous les mots collés au start_seconds."""
    segments = [{"start_seconds": 5.0, "end_seconds": 5.0, "text": "salut tous"}]
    words = flatten(segments)
    assert len(words) == 2
    assert all(w["s"] == 5.0 and w["e"] == 5.0 for w in words)


def test_segment_negative_duration_treated_as_zero():
    """Robustesse : end < start → on traite comme instantané."""
    segments = [{"start_seconds": 5.0, "end_seconds": 2.0, "text": "hi"}]
    words = flatten(segments)
    assert len(words) == 1
    assert words[0]["s"] == 5.0


def test_unicode_characters_preserved():
    """(f) Helpers robustes aux unicode."""
    segments = [{"start_seconds": 0.0, "end_seconds": 2.0, "text": "Café ☕ amer"}]
    words = flatten(segments)
    assert [w["w"] for w in words] == ["Café", "☕", "amer"]


def test_word_durations_proportional_to_length():
    """Mot long = plus de temps qu'un mot court."""
    # Texte 'a bbb cccccccc' : 1 / 3 / 8 chars → durée proportionnelle
    segments = [{"start_seconds": 0.0, "end_seconds": 12.0, "text": "a bbb cccccccc"}]
    words = flatten(segments)
    assert len(words) == 3
    d_a = words[0]["e"] - words[0]["s"]
    d_bbb = words[1]["e"] - words[1]["s"]
    d_long = words[2]["e"] - words[2]["s"]
    # Longueur 8 doit avoir une durée > longueur 3 > longueur 1.
    assert d_a < d_bbb < d_long


def test_invalid_segment_skipped_not_raised():
    """Robustesse aux segments malformés."""
    segments = [
        {"text": "no timestamps", "start_seconds": "not-a-number"},
        {"start_seconds": 0, "end_seconds": 1, "text": "valide"},
    ]
    words = flatten(segments)
    # Le 1er segment doit avoir été skipé proprement, le 2ème traité.
    assert [w["w"] for w in words] == ["valide"]


# ── concat_segments_text ───────────────────────────────────────────────

def test_concat_empty():
    assert concat([]) == ""
    assert concat(None) == ""  # type: ignore[arg-type]


def test_concat_strips_and_joins():
    segments = [
        {"text": "Bonjour"},
        {"text": "  le monde  "},
        {"text": ""},
        {"text": "comment ça va"},
    ]
    assert concat(segments) == "Bonjour le monde comment ça va"
