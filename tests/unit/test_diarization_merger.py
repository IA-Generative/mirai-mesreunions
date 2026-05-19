"""
Unit tests for the pure diarization+transcription merger.

The merger function takes a Whisper verbose_json transcription (with
segment-level timestamps) and a pyannote diarization (with speaker
intervals) and produces speaker-tagged Markdown. No HTTP, no LLM, no DB.
"""

import importlib.util
import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MODULE_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "diarization_merger.py")
SPEC = importlib.util.spec_from_file_location("diarization_merger_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)

merge_to_markdown = MOD.merge_to_markdown
list_unique_speakers = MOD.list_unique_speakers
_format_time = MOD._format_time
_assign_speaker = MOD._assign_speaker


# --- _format_time ---------------------------------------------------------

def test_format_time_under_a_minute():
    assert _format_time(45) == "0:45"


def test_format_time_under_an_hour():
    assert _format_time(125) == "2:05"


def test_format_time_with_hours():
    assert _format_time(3725) == "1:02:05"


def test_format_time_zero():
    assert _format_time(0) == "0:00"


# --- _assign_speaker ------------------------------------------------------

def test_assign_speaker_picks_largest_overlap():
    diar = [
        {"speaker": "SPEAKER_00", "start": 0.0, "end": 5.0},
        {"speaker": "SPEAKER_01", "start": 5.0, "end": 10.0},
    ]
    # Segment 4-6: 1s with SPEAKER_00, 1s with SPEAKER_01 → tie, first wins.
    assert _assign_speaker(4.0, 6.0, diar) == "SPEAKER_00"
    # Segment 5-9: fully under SPEAKER_01.
    assert _assign_speaker(5.0, 9.0, diar) == "SPEAKER_01"


def test_assign_speaker_fallback_when_no_diarization():
    assert _assign_speaker(0.0, 5.0, []) == "SPEAKER_00"


def test_assign_speaker_invalid_diar_entry_skipped():
    diar = [
        {"speaker": "SPEAKER_00", "start": "garbage", "end": 5.0},
        {"speaker": "SPEAKER_01", "start": 0.0, "end": 5.0},
    ]
    assert _assign_speaker(0.0, 5.0, diar) == "SPEAKER_01"


# --- merge_to_markdown ----------------------------------------------------

def test_merge_with_two_speakers_and_aligned_segments():
    transcription = {
        "duration": 32.0,
        "language": "fr",
        "text": "Bonjour à tous. Merci.",
        "segments": [
            {"start": 0.0, "end": 14.0, "text": "Bonjour à tous, on commence la réunion."},
            {"start": 14.0, "end": 32.0, "text": "Merci. Je vais présenter le sujet."},
        ],
    }
    diarization = {
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 14.0},
            {"speaker": "SPEAKER_01", "start": 14.0, "end": 32.0},
        ],
        "num_speakers": 2,
    }
    out = merge_to_markdown(transcription, diarization)
    # Raw pyannote SPEAKER_NN labels are rewritten to user-facing
    # Intervenant_NN at the rendering boundary.
    assert "**Intervenant_00**" in out
    assert "Bonjour à tous, on commence la réunion." in out
    assert "**Intervenant_01**" in out
    assert "Merci. Je vais présenter le sujet." in out
    assert "**SPEAKER_" not in out
    # Two distinct blocks, separated by blank line.
    assert out.count("**Intervenant_") == 2


def test_merge_groups_consecutive_segments_of_same_speaker():
    """Two adjacent Whisper segments from the same speaker → one merged block."""
    transcription = {
        "duration": 30.0,
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "Première phrase."},
            {"start": 10.0, "end": 20.0, "text": "Deuxième phrase."},
            {"start": 20.0, "end": 30.0, "text": "Troisième phrase de l'autre intervenant."},
        ],
    }
    diarization = {
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 20.0},
            {"speaker": "SPEAKER_01", "start": 20.0, "end": 30.0},
        ],
    }
    out = merge_to_markdown(transcription, diarization)
    # Intervenant_00 block contains BOTH first and second sentence.
    assert "Première phrase." in out
    assert "Deuxième phrase." in out
    # And only TWO blocks total (one per speaker).
    assert out.count("**Intervenant_") == 2


def test_merge_substitutes_real_speaker_names():
    transcription = {
        "duration": 20.0,
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "Bonjour."},
            {"start": 10.0, "end": 20.0, "text": "Bonjour à toi."},
        ],
    }
    diarization = {
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
            {"speaker": "SPEAKER_01", "start": 10.0, "end": 20.0},
        ],
    }
    out = merge_to_markdown(transcription, diarization, speaker_names={
        "SPEAKER_00": "Jean Dupont",
        "SPEAKER_01": "Marie Curie",
    })
    assert "**Jean Dupont**" in out
    assert "**Marie Curie**" in out
    assert "**SPEAKER_" not in out
    assert "**Intervenant_" not in out


def test_merge_partial_naming_keeps_unmatched_anonymous():
    transcription = {
        "duration": 20.0,
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "A."},
            {"start": 10.0, "end": 20.0, "text": "B."},
        ],
    }
    diarization = {
        "segments": [
            {"speaker": "SPEAKER_00", "start": 0.0, "end": 10.0},
            {"speaker": "SPEAKER_01", "start": 10.0, "end": 20.0},
        ],
    }
    out = merge_to_markdown(transcription, diarization, speaker_names={"SPEAKER_00": "Jean"})
    assert "**Jean**" in out
    assert "**Intervenant_01**" in out  # not resolved → rewritten from SPEAKER_01
    assert "**SPEAKER_" not in out


def test_merge_empty_transcription_returns_empty_string():
    assert merge_to_markdown({"segments": []}, {"segments": []}) == ""
    assert merge_to_markdown({}, {}) == ""


def test_merge_no_segment_timestamps_falls_back_to_single_block():
    """When transcription has only top-level text (plain json, no verbose_json)."""
    transcription = {"text": "Bonjour à tous.", "duration": 5.0}
    diarization = {"segments": []}
    out = merge_to_markdown(transcription, diarization)
    assert "**Intervenant_00**" in out
    assert "Bonjour à tous." in out
    assert "(0:00 → 0:05)" in out


def test_merge_no_diarization_assigns_all_to_speaker_00():
    transcription = {
        "duration": 20.0,
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "A."},
            {"start": 10.0, "end": 20.0, "text": "B."},
        ],
    }
    out = merge_to_markdown(transcription, {"segments": []})
    # Single Intervenant_00 block (consecutive segments grouped).
    assert out.count("**Intervenant_") == 1
    assert "A." in out and "B." in out


def test_merge_skips_empty_segment_text():
    transcription = {
        "duration": 20.0,
        "segments": [
            {"start": 0.0, "end": 5.0, "text": ""},
            {"start": 5.0, "end": 10.0, "text": "Réelle phrase."},
            {"start": 10.0, "end": 20.0, "text": "   "},
        ],
    }
    out = merge_to_markdown(transcription, {"segments": []})
    assert "Réelle phrase." in out
    # Only one block — empty segments dropped.
    assert out.count("**Intervenant_") == 1


# --- list_unique_speakers ------------------------------------------------

def test_list_unique_speakers_preserves_first_seen_order():
    diar = {"segments": [
        {"speaker": "SPEAKER_01", "start": 0, "end": 1},
        {"speaker": "SPEAKER_00", "start": 1, "end": 2},
        {"speaker": "SPEAKER_01", "start": 2, "end": 3},
        {"speaker": "SPEAKER_02", "start": 3, "end": 4},
    ]}
    assert list_unique_speakers(diar) == ["SPEAKER_01", "SPEAKER_00", "SPEAKER_02"]


def test_list_unique_speakers_empty():
    assert list_unique_speakers({"segments": []}) == []
    assert list_unique_speakers({}) == []
