"""
Unit tests for the glossary loader + relevance filter used by the
``glossary_correction`` step of the Kevent meeting-intelligence pipeline.
"""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


MODULE_PATH = os.path.join(ROOT, "services", "file-mover", "app", "glossary_loader.py")
SPEC = importlib.util.spec_from_file_location("glossary_loader_under_test", MODULE_PATH)
GL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GL)


# ─── load_glossary_dir ─────────────────────────────────────────────────────

def test_load_missing_directory_returns_empty_no_raise():
    assert GL.load_glossary_dir("/nonexistent/path/abc123") == []


def test_load_md_extracts_bold_terms():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "g.md").write_text(
            "**ANSC** - Agence numérique sécurité civile - définition\n\n"
            "**ANTAI** ou **AC** - Agence ... - autre def\n",
            encoding="utf-8",
        )
        terms = GL.load_glossary_dir(tmp)
    # Order = first-seen, dedup
    assert "ANSC" in terms
    assert "ANTAI" in terms
    assert "AC" in terms
    # No definition fragments
    assert all("Agence" not in t for t in terms)


def test_load_txt_one_term_per_line_skips_comments_and_blank():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "g.txt").write_text(
            "# header comment\n\nDAGEM\nANSC\n\n# another comment\nDGSI\n",
            encoding="utf-8",
        )
        terms = GL.load_glossary_dir(tmp)
    assert terms == ["DAGEM", "ANSC", "DGSI"]


def test_load_json_supports_strings_and_dicts():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "g.json").write_text(json.dumps([
            "DAGEM",
            {"term": "ANSC", "definition": "ignored"},
            {"name": "DGSI"},
            12345,  # ignored
            {"other": "ignored"},  # ignored (no term/name)
        ]), encoding="utf-8")
        terms = GL.load_glossary_dir(tmp)
    assert terms == ["DAGEM", "ANSC", "DGSI"]


def test_load_dedups_across_files():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.md").write_text("**ANSC** - a\n", encoding="utf-8")
        Path(tmp, "b.txt").write_text("ANSC\nDGSI\n", encoding="utf-8")
        terms = GL.load_glossary_dir(tmp)
    assert terms == ["ANSC", "DGSI"]


def test_load_invalid_json_logged_and_skipped(caplog):
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "good.md").write_text("**OK** - x\n", encoding="utf-8")
        Path(tmp, "bad.json").write_text("{not valid json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            terms = GL.load_glossary_dir(tmp)
    assert "OK" in terms
    # The bad file was skipped, not crashed
    assert len(terms) == 1


def test_load_ignores_unknown_extensions():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "g.md").write_text("**ONLY** - x\n", encoding="utf-8")
        Path(tmp, "g.bin").write_text("not a glossary", encoding="utf-8")
        Path(tmp, "g.yaml").write_text("not parsed", encoding="utf-8")
        terms = GL.load_glossary_dir(tmp)
    assert terms == ["ONLY"]


# ─── filter_relevant ───────────────────────────────────────────────────────

def test_filter_relevant_picks_substring_match():
    terms = ["ANSC", "DGSI", "DAGEM"]
    transcript = "Le DGSI a transmis le rapport à l'ANSC."
    out = GL.filter_relevant(terms, transcript, max_terms=10)
    assert "DGSI" in out
    assert "ANSC" in out
    # DAGEM not mentioned in any form → excluded
    assert "DAGEM" not in out


def test_filter_relevant_picks_spelled_out_letters():
    """Whisper writes "D A G E M" letter by letter — should still match DAGEM."""
    terms = ["DAGEM", "DGSI", "UNRELATED"]
    transcript = "Le bureau d a g e m a validé le dossier."
    out = GL.filter_relevant(terms, transcript, max_terms=10)
    assert "DAGEM" in out


def test_filter_relevant_returns_empty_when_no_match():
    terms = ["XYZ", "ABCD"]
    transcript = "Bonjour à tous, on commence la réunion."
    assert GL.filter_relevant(terms, transcript, max_terms=10) == []


def test_filter_relevant_caps_at_max_terms():
    terms = [f"TERM_{i}" for i in range(50)]
    # All match by spelled-out letters in this junk transcript:
    transcript = " ".join("t e r m _ " + str(i) for i in range(50))
    out = GL.filter_relevant(terms, transcript, max_terms=5)
    assert len(out) == 5


def test_filter_relevant_empty_inputs_safe():
    assert GL.filter_relevant([], "anything", 10) == []
    assert GL.filter_relevant(["X"], "", 10) == []


def test_filter_relevant_results_are_sorted_for_cache_friendliness():
    terms = ["ZETA", "ALPHA", "BETA"]
    transcript = "le z e t a et le b e t a et le a l p h a"
    out = GL.filter_relevant(terms, transcript, max_terms=10)
    assert out == sorted(out)
