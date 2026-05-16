"""
Unit tests for the ``apply_glossary_correction`` step in meeting_intelligence.
Verifies it is best-effort like the other steps: skips when nothing relevant,
returns None on LLM error, and propagates the LLM output otherwise.
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


# Stub `requests` (needed when llm_client imports it at module load).
_REQ = types.ModuleType("requests")
class _RE(Exception):
    pass
_REQ.RequestException = _RE
_REQ.post = MagicMock()
sys.modules["requests"] = _REQ

# Load llm_client and meeting_intelligence in the same way the existing
# meeting_intelligence tests do.
LLM_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "llm_client.py")
_lspec = importlib.util.spec_from_file_location("app.llm_client", LLM_PATH)
LLM_MOD = importlib.util.module_from_spec(_lspec)
sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules["app.llm_client"] = LLM_MOD
_lspec.loader.exec_module(LLM_MOD)

# Load the real glossary_loader (no stub — its filter_relevant is used by the
# step). The deferred import inside apply_glossary_correction is `from
# app.glossary_loader import filter_relevant` — so we must register it under
# that name in sys.modules.
GL_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "glossary_loader.py")
_gspec = importlib.util.spec_from_file_location("app.glossary_loader", GL_PATH)
GL_MOD = importlib.util.module_from_spec(_gspec)
sys.modules["app.glossary_loader"] = GL_MOD
_gspec.loader.exec_module(GL_MOD)

MI_PATH = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "meeting_intelligence.py")
_mspec = importlib.util.spec_from_file_location("meeting_intelligence_under_test", MI_PATH)
MI = importlib.util.module_from_spec(_mspec)
_mspec.loader.exec_module(MI)


def _llm_returning(content: str):
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat.return_value = content
    return fake


def _llm_raising(exc):
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat.side_effect = exc
    return fake


GLOSSARY = ["ANSC", "DGSI", "DAGEM"]


# ─── apply_glossary_correction ────────────────────────────────────────────

def test_correction_returns_llm_output_on_success():
    fake = _llm_returning("Le DGSI a transmis le rapport à l'ANSC.")
    out = MI.apply_glossary_correction(
        "Le D G S I a transmis le rapport à l'ANSC.",
        fake, "model-medium", glossary_terms=GLOSSARY,
    )
    assert out == "Le DGSI a transmis le rapport à l'ANSC."


def test_correction_passes_only_relevant_terms_to_llm():
    """The prompt should not contain unrelated glossary entries."""
    fake = _llm_returning("xx")
    MI.apply_glossary_correction(
        "Le DGSI a fait son rapport.",
        fake, "model-medium", glossary_terms=GLOSSARY + ["UNRELATED_XYZ"],
    )
    # Inspect the prompt that was actually sent
    args, kwargs = fake.chat.call_args
    sent_prompt = args[1][0]["content"]
    assert "DGSI" in sent_prompt
    # UNRELATED_XYZ has no signal in transcript → must not appear
    assert "UNRELATED_XYZ" not in sent_prompt


def test_correction_returns_none_when_no_relevant_terms():
    """If the filter selects nothing, we don't even call the LLM."""
    fake = _llm_returning("should not be returned")
    out = MI.apply_glossary_correction(
        "Bonjour à tous, on commence.",
        fake, "model-medium", glossary_terms=["XYZ", "WXYZ"],  # no match
    )
    assert out is None
    fake.chat.assert_not_called()


def test_correction_returns_none_on_llm_error():
    fake = _llm_raising(LLM_MOD.LLMTransientError("network fail"))
    out = MI.apply_glossary_correction(
        "Le DGSI a transmis", fake, "m", glossary_terms=GLOSSARY,
    )
    assert out is None


def test_correction_empty_transcript_skips_llm():
    fake = _llm_returning("xxx")
    assert MI.apply_glossary_correction("", fake, "m", glossary_terms=GLOSSARY) is None
    fake.chat.assert_not_called()


def test_correction_empty_glossary_skips_llm():
    fake = _llm_returning("xxx")
    assert MI.apply_glossary_correction("anything", fake, "m", glossary_terms=[]) is None
    fake.chat.assert_not_called()


def test_correction_respects_max_terms_per_call():
    fake = _llm_returning("xx")
    huge_glossary = [f"TERM_{i:03d}" for i in range(50)]
    transcript = " ".join("t e r m _ " + f"{i:03d}" for i in range(50))
    MI.apply_glossary_correction(
        transcript, fake, "m",
        glossary_terms=huge_glossary, max_terms_per_call=3,
    )
    args, kwargs = fake.chat.call_args
    sent_prompt = args[1][0]["content"]
    # Count actual terms in the prompt block (lines starting with "- TERM_")
    n_terms_in_prompt = sum(1 for line in sent_prompt.splitlines() if line.startswith("- TERM_"))
    assert n_terms_in_prompt == 3
