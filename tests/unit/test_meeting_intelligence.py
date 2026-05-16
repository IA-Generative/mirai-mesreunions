"""
Unit tests for the meeting-intelligence orchestration. Each step is
best-effort: the tests confirm that LLM failures map to graceful
fallbacks (None / {} / no exception) rather than aborting the pipeline.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# Need to install a stubbed app.llm_client before loading the orchestration.
_REQ = types.ModuleType("requests")
class _RE(Exception): pass
_REQ.RequestException = _RE
_REQ.post = MagicMock()
sys.modules["requests"] = _REQ

# Load llm_client first (gives us the LLMError class hierarchy).
LLM_CLIENT_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "llm_client.py")
_llm_spec = importlib.util.spec_from_file_location("app.llm_client", LLM_CLIENT_PATH)
LLM_MOD = importlib.util.module_from_spec(_llm_spec)
sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules["app.llm_client"] = LLM_MOD
_llm_spec.loader.exec_module(LLM_MOD)

# Now load meeting_intelligence — its `from app.llm_client import LLMClient, LLMError`
# resolves against the stub above.
MI_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "meeting_intelligence.py")
SPEC = importlib.util.spec_from_file_location("meeting_intelligence_under_test", MI_PATH)
MI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MI)


def _llm_returning(content: str = "ok"):
    """Build a mock LLMClient whose chat() returns the given string."""
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat.return_value = content
    fake.chat_json.return_value = {}
    return fake


def _llm_raising(exc):
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat.side_effect = exc
    fake.chat_json.side_effect = exc
    return fake


# --- prompts loading ------------------------------------------------------

def test_prompts_directory_contains_required_files():
    """The pipeline depends on these prompts being shipped with the image."""
    prompts_dir = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "prompts")
    expected = {
        "speaker_names.txt",
        "oob_cleaning.txt",
        "reformulation.txt",
        "meeting_analysis.txt",
        "absentee_summary.txt",
    }
    actual = set(os.listdir(prompts_dir))
    missing = expected - actual
    assert not missing, f"Missing prompts: {missing}"


def test_prompts_contain_transcript_placeholder():
    """All prompts must use the {TRANSCRIPT} placeholder so the orchestrator can substitute."""
    prompts_dir = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "prompts")
    for name in (
        "speaker_names.txt",
        "oob_cleaning.txt",
        "reformulation.txt",
        "meeting_analysis.txt",
        "absentee_summary.txt",
    ):
        with open(os.path.join(prompts_dir, name), encoding="utf-8") as f:
            content = f.read()
        assert "{TRANSCRIPT}" in content, f"{name} missing the {{TRANSCRIPT}} placeholder"


# --- extract_speaker_names -----------------------------------------------

def test_extract_speaker_names_returns_mapping_on_success():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.return_value = {"SPEAKER_00": "Jean", "SPEAKER_01": "Marie"}
    out = MI.extract_speaker_names("**SPEAKER_00** ...", fake, "model-x")
    assert out == {"SPEAKER_00": "Jean", "SPEAKER_01": "Marie"}


def test_extract_speaker_names_filters_non_speaker_keys():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.return_value = {
        "SPEAKER_00": "Jean",
        "RANDOM_KEY": "noise",
        "SPEAKER_01": "",
    }
    out = MI.extract_speaker_names("xx", fake, "m")
    assert out == {"SPEAKER_00": "Jean"}


def test_extract_speaker_names_filters_self_referential_values():
    """If LLM couldn't determine a name, it's instructed to keep SPEAKER_NN — drop those."""
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.return_value = {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "Marie"}
    out = MI.extract_speaker_names("xx", fake, "m")
    assert out == {"SPEAKER_01": "Marie"}


def test_extract_speaker_names_returns_empty_on_llm_error():
    fake = _llm_raising(LLM_MOD.LLMTransientError("network"))
    out = MI.extract_speaker_names("xx", fake, "m")
    assert out == {}


def test_extract_speaker_names_returns_empty_on_non_dict_response():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.return_value = ["not a dict"]  # buggy LLM
    out = MI.extract_speaker_names("xx", fake, "m")
    assert out == {}


def test_extract_speaker_names_empty_transcript_skips_llm():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    out = MI.extract_speaker_names("", fake, "m")
    assert out == {}
    fake.chat_json.assert_not_called()


# --- clean_oob ------------------------------------------------------------

def test_clean_oob_returns_llm_output():
    fake = _llm_returning("texte nettoyé")
    assert MI.clean_oob("xx", fake, "m") == "texte nettoyé"


def test_clean_oob_returns_none_on_llm_error():
    fake = _llm_raising(LLM_MOD.LLMApplicativeError("context too long"))
    assert MI.clean_oob("xx", fake, "m") is None


def test_clean_oob_empty_transcript_returns_none():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    assert MI.clean_oob("", fake, "m") is None
    fake.chat.assert_not_called()


# --- reformulate ----------------------------------------------------------

def test_reformulate_returns_llm_output():
    fake = _llm_returning("Jean a dit que…")
    assert MI.reformulate("xx", fake, "m") == "Jean a dit que…"


def test_reformulate_returns_none_on_llm_error():
    fake = _llm_raising(LLM_MOD.LLMTransientError("timeout"))
    assert MI.reformulate("xx", fake, "m") is None


# --- analyse_meeting ------------------------------------------------------

def test_analyse_meeting_returns_dict_on_success():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.return_value = {
        "actors": [{"name": "Jean", "role": None}],
        "themes": [],
        "decisions": [],
        "gaps": [],
        "recommendations": [],
    }
    out = MI.analyse_meeting("xx", fake, "m")
    assert out is not None
    assert "actors" in out


def test_analyse_meeting_returns_none_on_invalid_json():
    fake = _llm_raising(LLM_MOD.LLMApplicativeError("invalid json"))
    assert MI.analyse_meeting("xx", fake, "m") is None


def test_analyse_meeting_empty_transcript_returns_none():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    assert MI.analyse_meeting("", fake, "m") is None
    fake.chat_json.assert_not_called()


# --- serialize_analysis --------------------------------------------------

def test_serialize_analysis_returns_json_string():
    out = MI.serialize_analysis({"actors": [{"name": "Jean"}]})
    import json
    parsed = json.loads(out)
    assert parsed["actors"][0]["name"] == "Jean"


def test_serialize_analysis_none_passthrough():
    assert MI.serialize_analysis(None) is None


# --- summarise_for_absentee ----------------------------------------------

def test_summarise_for_absentee_returns_llm_output():
    fake = _llm_returning("Voici un débrief pour les absents...")
    assert MI.summarise_for_absentee("xx", fake, "m") == "Voici un débrief pour les absents..."


def test_summarise_for_absentee_returns_none_on_llm_error():
    fake = _llm_raising(LLM_MOD.LLMTransientError("timeout"))
    assert MI.summarise_for_absentee("xx", fake, "m") is None


def test_summarise_for_absentee_empty_transcript_returns_none():
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    assert MI.summarise_for_absentee("", fake, "m") is None
    fake.chat.assert_not_called()


def test_summarise_for_absentee_uses_chat_not_chat_json():
    """The absentee debrief is plain prose, not structured JSON — must call chat()."""
    fake = _llm_returning("texte libre")
    MI.summarise_for_absentee("transcript content", fake, "model-medium")
    fake.chat.assert_called_once()
    fake.chat_json.assert_not_called()


def test_summarise_for_absentee_passes_transcript_into_prompt():
    """The prompt template must receive the transcript via {TRANSCRIPT} substitution."""
    fake = _llm_returning("ok")
    MI.summarise_for_absentee("CONTENU UNIQUE 123", fake, "m")
    sent_messages = fake.chat.call_args.args[1] if fake.chat.call_args.args else fake.chat.call_args.kwargs["messages"]
    assert "CONTENU UNIQUE 123" in sent_messages[0]["content"]
