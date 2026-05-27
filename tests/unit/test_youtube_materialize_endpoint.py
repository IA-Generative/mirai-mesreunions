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
