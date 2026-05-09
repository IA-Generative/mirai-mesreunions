"""
Unit tests for the transcript format converters used by the code-generator
download endpoints (.txt / .md / .docx / .odt). Skips DOCX/ODT tests when
python-docx / odfpy are not installed locally.
"""

import importlib.util
import json
import os
import sys
from io import BytesIO

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


MOD_PATH = os.path.join(ROOT, "services", "code-generator", "app", "transcript_formats.py")
SPEC = importlib.util.spec_from_file_location("transcript_formats_under_test", MOD_PATH)
TF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TF)


# ─── meeting_analysis_to_markdown ───────────────────────────────────────────

def test_md_renders_all_5_sections():
    analysis = {
        "actors": [{"name": "Jean", "role": "PM"}],
        "themes": [{"title": "Budget", "summary": "Q3"}],
        "decisions": [{"item": "Valider", "owner": "Jean", "due": "2026-06-01"}],
        "gaps": ["Roadmap manquante"],
        "recommendations": ["Faire un brief COMEX"],
    }
    md = TF.meeting_analysis_to_markdown(analysis)
    assert "# Compte-rendu de réunion" in md
    assert "## Acteurs présents" in md
    assert "## Thématiques abordées" in md
    assert "## Décisions et points en action" in md
    assert "## Sujets non abordés" in md
    assert "## Recommandations" in md
    # bullets for list values
    assert "- " in md


def test_md_skips_empty_sections():
    md = TF.meeting_analysis_to_markdown({"actors": [{"name": "Jean"}], "themes": []})
    assert "## Acteurs présents" in md
    assert "## Thématiques abordées" not in md  # empty list → skipped


def test_md_accepts_json_string():
    raw = json.dumps({"actors": ["Jean"]})
    md = TF.meeting_analysis_to_markdown(raw)
    assert "Jean" in md


def test_md_passthrough_on_invalid_json():
    md = TF.meeting_analysis_to_markdown("not json at all")
    assert md == "not json at all"


def test_md_handles_non_dict_gracefully():
    assert TF.meeting_analysis_to_markdown(["a", "b"]) == ""
    assert TF.meeting_analysis_to_markdown(42) == ""


# ─── DOCX conversion ────────────────────────────────────────────────────────

@pytest.fixture
def has_docx():
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


def test_docx_produces_valid_zip_blob(has_docx):
    if not has_docx:
        pytest.skip("python-docx not installed")
    blob = TF.text_to_docx_bytes("# Title\n\n- bullet 1\n- bullet 2\n\nplain para.", title="t")
    # .docx is a ZIP — first bytes should be PK\x03\x04
    assert blob[:4] == b"PK\x03\x04"
    assert len(blob) > 1000  # non-trivial file


def test_docx_handles_empty_input(has_docx):
    if not has_docx:
        pytest.skip("python-docx not installed")
    blob = TF.text_to_docx_bytes("", title="empty")
    assert blob[:4] == b"PK\x03\x04"


# ─── ODT conversion ─────────────────────────────────────────────────────────

@pytest.fixture
def has_odf():
    try:
        import odf  # noqa: F401
        return True
    except ImportError:
        return False


def test_odt_produces_valid_zip_blob(has_odf):
    if not has_odf:
        pytest.skip("odfpy not installed")
    blob = TF.text_to_odt_bytes("# Title\n\n- bullet 1\n\nplain para.", title="t")
    assert blob[:4] == b"PK\x03\x04"  # ODT is also a ZIP
    assert len(blob) > 1000


def test_odt_handles_headings_and_bullets(has_odf):
    if not has_odf:
        pytest.skip("odfpy not installed")
    blob = TF.text_to_odt_bytes("## h2\n# h1\n- a\n- b\n\nfin.", title="x")
    assert blob[:4] == b"PK\x03\x04"


# ─── Markdown block iterator ────────────────────────────────────────────────

def test_iter_blocks_classifies_headings_bullets_paras():
    blocks = list(TF._iter_blocks("# H1\n## H2\n- bullet\nparagraph\n\n* alt-bullet"))
    kinds = [b[0] for b in blocks]
    assert kinds == ["heading", "heading", "bullet", "para", "blank", "bullet"]
    assert blocks[0] == ("heading", 1, "H1")
    assert blocks[1] == ("heading", 2, "H2")
    assert blocks[2] == ("bullet", 0, "bullet")
