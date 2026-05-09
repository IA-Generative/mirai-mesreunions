"""
Unit tests for the ``suggest_metadata`` step (one chat-small LLM call →
short title + key_points list, used by Feature 3 user-facing downloads).
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


# Stub `requests` (used by llm_client at module load).
_REQ = types.ModuleType("requests")
class _RE(Exception):
    pass
_REQ.RequestException = _RE
_REQ.post = MagicMock()
sys.modules["requests"] = _REQ

LLM_PATH = os.path.join(ROOT, "services", "file-mover", "app", "llm_client.py")
_lspec = importlib.util.spec_from_file_location("app.llm_client", LLM_PATH)
LLM_MOD = importlib.util.module_from_spec(_lspec)
sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules["app.llm_client"] = LLM_MOD
_lspec.loader.exec_module(LLM_MOD)

MI_PATH = os.path.join(ROOT, "services", "file-mover", "app", "meeting_intelligence.py")
_mspec = importlib.util.spec_from_file_location("meeting_intelligence_under_test", MI_PATH)
MI = importlib.util.module_from_spec(_mspec)
_mspec.loader.exec_module(MI)


def _llm_returning_json(payload: dict):
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.return_value = payload
    return fake


def _llm_raising(exc):
    fake = MagicMock(spec=LLM_MOD.LLMClient)
    fake.chat_json.side_effect = exc
    return fake


# ─── suggest_metadata ──────────────────────────────────────────────────────

def test_returns_title_and_key_points_on_success():
    fake = _llm_returning_json({
        "title": "Réunion budget Q3",
        "key_points": ["Validation budget", "Préparer brief COMEX", "Risque délai"],
    })
    out = MI.suggest_metadata("Long transcript...", fake, "chat-small")
    assert out["title"] == "Réunion budget Q3"
    assert out["key_points"] == ["Validation budget", "Préparer brief COMEX", "Risque délai"]


def test_strips_forbidden_filename_chars():
    fake = _llm_returning_json({"title": "Réu/nion : budget?", "key_points": []})
    out = MI.suggest_metadata("xx", fake, "m")
    # `/` `:` `?` are forbidden — must be removed (replaced with spaces, then collapsed)
    assert "/" not in out["title"]
    assert ":" not in out["title"]
    assert "?" not in out["title"]


def test_caps_title_length_at_80_chars():
    long = "X" * 200
    fake = _llm_returning_json({"title": long, "key_points": []})
    out = MI.suggest_metadata("xx", fake, "m")
    assert len(out["title"]) <= 80


def test_falls_back_to_compte_rendu_on_empty_title():
    fake = _llm_returning_json({"title": "   ", "key_points": []})
    out = MI.suggest_metadata("xx", fake, "m")
    assert out["title"] == "Compte-rendu"


def test_caps_key_points_at_5():
    fake = _llm_returning_json({
        "title": "T",
        "key_points": [f"point {i}" for i in range(20)],
    })
    out = MI.suggest_metadata("xx", fake, "m")
    assert len(out["key_points"]) == 5


def test_filters_non_string_key_points():
    fake = _llm_returning_json({
        "title": "T",
        "key_points": ["valid", 42, None, {"x": "y"}, "  ", "another"],
    })
    out = MI.suggest_metadata("xx", fake, "m")
    assert out["key_points"] == ["valid", "another"]


def test_returns_none_on_empty_transcript():
    fake = _llm_returning_json({"title": "x", "key_points": []})
    assert MI.suggest_metadata("", fake, "m") is None
    assert MI.suggest_metadata("   ", fake, "m") is None
    fake.chat_json.assert_not_called()


def test_returns_none_on_llm_error():
    fake = _llm_raising(LLM_MOD.LLMTransientError("network"))
    assert MI.suggest_metadata("xx", fake, "m") is None


def test_returns_none_on_non_dict_response():
    fake = _llm_returning_json(["not a dict"])  # buggy LLM
    assert MI.suggest_metadata("xx", fake, "m") is None


def test_handles_missing_key_points_key():
    fake = _llm_returning_json({"title": "OK"})  # no key_points
    out = MI.suggest_metadata("xx", fake, "m")
    assert out["title"] == "OK"
    assert out["key_points"] == []


# ─── serialize_key_points ──────────────────────────────────────────────────

def test_serialize_key_points_returns_md_bullets():
    out = MI.serialize_key_points(["a", "b", "c"])
    assert out == "- a\n- b\n- c"


def test_serialize_key_points_empty_returns_none():
    assert MI.serialize_key_points([]) is None
    assert MI.serialize_key_points(None) is None
