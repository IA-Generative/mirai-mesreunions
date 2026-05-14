"""
Unit tests for services.file-mover.app.doc_extractor.

The extractor wraps four third-party libs (pypdf, python-docx, odfpy,
python-pptx). The tests stub those libs in ``sys.modules`` so we exercise
our routing / dispatch / error-handling logic without requiring the real
libraries to be installed or shipping binary fixtures.

What we care about:
  - MIME → format dispatch (and filename fallback when MIME is generic)
  - never-raise contract: corrupted / encrypted / image-only / unsupported
    documents all return ``""`` rather than blowing up the caller
  - per-format reader output is concatenated, normalized, and capped
"""

import importlib.util
import io
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# The module under test imports its four parser libraries lazily (inside
# the per-format helpers), so we can load it before installing stubs.
MODULE_PATH = os.path.join(ROOT, "services", "file-mover", "app", "doc_extractor.py")
SPEC = importlib.util.spec_from_file_location("doc_extractor_under_test", MODULE_PATH)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MOD)


# Names we replace in ``sys.modules`` while a test runs. Saved-and-restored
# per test so other test files in the suite (notably test_transcript_formats,
# which uses the real odfpy) keep seeing the genuine libraries.
_STUB_NAMES = (
    "pypdf", "pypdf.errors",
    "docx",
    "pptx",
    "odf", "odf.opendocument", "odf.teletype", "odf.text", "odf.draw",
)


# Module-level holder populated by the fixture; tests reference it directly
# without taking the fixture as a parameter.
_STUBS: dict = {}


@pytest.fixture(autouse=True)
def _parser_stubs():
    saved = {n: sys.modules.get(n) for n in _STUB_NAMES}

    # ---- pypdf -------------------------------------------------
    pypdf_mod = types.ModuleType("pypdf")
    pypdf_errors_mod = types.ModuleType("pypdf.errors")

    class PdfReadError(Exception):
        pass

    pypdf_errors_mod.PdfReadError = PdfReadError
    pypdf_mod.errors = pypdf_errors_mod
    pypdf_mod.PdfReader = MagicMock()
    sys.modules["pypdf"] = pypdf_mod
    sys.modules["pypdf.errors"] = pypdf_errors_mod

    # ---- python-docx ------------------------------------------
    docx_mod = types.ModuleType("docx")
    docx_mod.Document = MagicMock()
    sys.modules["docx"] = docx_mod

    # ---- python-pptx ------------------------------------------
    pptx_mod = types.ModuleType("pptx")
    pptx_mod.Presentation = MagicMock()
    sys.modules["pptx"] = pptx_mod

    # ---- odfpy (odf.opendocument, odf.teletype, odf.text, odf.draw) -
    odf_pkg = types.ModuleType("odf")
    odf_opendocument = types.ModuleType("odf.opendocument")
    odf_teletype = types.ModuleType("odf.teletype")
    odf_text = types.ModuleType("odf.text")
    odf_draw = types.ModuleType("odf.draw")

    odf_opendocument.load = MagicMock()
    odf_teletype.extractText = MagicMock()

    # Sentinel classes — getElementsByType() compares by these.
    class _P: ...
    class _H: ...
    class _TextBox: ...

    odf_text.P = _P
    odf_text.H = _H
    odf_draw.TextBox = _TextBox

    odf_pkg.opendocument = odf_opendocument
    odf_pkg.teletype = odf_teletype
    odf_pkg.text = odf_text
    odf_pkg.draw = odf_draw
    sys.modules["odf"] = odf_pkg
    sys.modules["odf.opendocument"] = odf_opendocument
    sys.modules["odf.teletype"] = odf_teletype
    sys.modules["odf.text"] = odf_text
    sys.modules["odf.draw"] = odf_draw

    _STUBS.update({
        "pypdf": pypdf_mod,
        "PdfReadError": PdfReadError,
        "docx": docx_mod,
        "pptx": pptx_mod,
        "odf_opendocument": odf_opendocument,
        "odf_teletype": odf_teletype,
        "odf_text": odf_text,
        "odf_draw": odf_draw,
    })

    yield

    _STUBS.clear()
    for name, original in saved.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


# ─── Format detection ────────────────────────────────────────────

def test_empty_content_returns_empty_string_without_dispatching():
    assert MOD.extract_text(b"", "application/pdf") == ""
    assert _STUBS["pypdf"].PdfReader.call_count == 0


def test_unsupported_mime_returns_empty():
    assert MOD.extract_text(b"...", "application/x-tar") == ""


def test_unknown_mime_with_known_extension_is_skipped():
    # MIME is specific (image/png) so we do NOT fall back to the .pdf
    # extension — the document really is a PNG mislabelled as PDF.
    assert MOD.extract_text(b"...", "image/png", filename_hint="trap.pdf") == ""


def test_generic_octet_stream_falls_back_to_extension():
    _STUBS["docx"].Document.return_value = MagicMock(paragraphs=[_p("hello")], tables=[])
    out = MOD.extract_text(b"...", "application/octet-stream", filename_hint="brief.docx")
    assert out == "hello"


def test_empty_mime_falls_back_to_extension():
    _STUBS["docx"].Document.return_value = MagicMock(paragraphs=[_p("hello")], tables=[])
    assert MOD.extract_text(b"...", "", filename_hint="brief.docx") == "hello"


def test_mime_with_charset_parameter_is_recognized():
    assert MOD.extract_text(b"abc", "text/plain; charset=utf-8") == "abc"


# ─── PDF ────────────────────────────────────────────────────────

def _pdf_reader(pages_text, is_encrypted=False, decrypt_raises=False):
    pages = []
    for t in pages_text:
        page = MagicMock()
        page.extract_text.return_value = t
        pages.append(page)
    reader = MagicMock()
    reader.pages = pages
    reader.is_encrypted = is_encrypted
    if decrypt_raises:
        reader.decrypt.side_effect = Exception("bad password")
    return reader


def test_pdf_concatenates_pages_with_blank_line():
    _STUBS["pypdf"].PdfReader.return_value = _pdf_reader(["page one text", "page two text"])
    out = MOD.extract_text(b"%PDF-...", "application/pdf")
    assert "page one text" in out
    assert "page two text" in out
    # Pages are joined with a blank line separator.
    assert "page one text\n\npage two text" in out


def test_pdf_image_only_returns_empty():
    _STUBS["pypdf"].PdfReader.return_value = _pdf_reader(["", "", ""])
    assert MOD.extract_text(b"%PDF-...", "application/pdf") == ""


def test_pdf_invalid_header_returns_empty():
    _STUBS["pypdf"].PdfReader.side_effect = _STUBS["PdfReadError"]("not a pdf")
    assert MOD.extract_text(b"not a pdf", "application/pdf") == ""


def test_pdf_encrypted_with_empty_password_unlocks():
    reader = _pdf_reader(["secret content"], is_encrypted=True)
    reader.decrypt.return_value = 1  # success
    _STUBS["pypdf"].PdfReader.return_value = reader
    assert "secret content" in MOD.extract_text(b"%PDF-...", "application/pdf")
    reader.decrypt.assert_called_once_with("")


def test_pdf_encrypted_password_unknown_returns_empty():
    reader = _pdf_reader(["secret content"], is_encrypted=True, decrypt_raises=True)
    _STUBS["pypdf"].PdfReader.return_value = reader
    assert MOD.extract_text(b"%PDF-...", "application/pdf") == ""


def test_pdf_page_extract_raises_is_swallowed():
    page_ok = MagicMock()
    page_ok.extract_text.return_value = "good page"
    page_bad = MagicMock()
    page_bad.extract_text.side_effect = RuntimeError("font table broken")
    reader = MagicMock()
    reader.pages = [page_ok, page_bad]
    reader.is_encrypted = False
    _STUBS["pypdf"].PdfReader.return_value = reader
    assert MOD.extract_text(b"%PDF-...", "application/pdf") == "good page"


def test_pdf_generic_parse_error_returns_empty():
    _STUBS["pypdf"].PdfReader.side_effect = RuntimeError("zlib decompress failed")
    assert MOD.extract_text(b"corrupt", "application/pdf") == ""


# ─── DOCX ───────────────────────────────────────────────────────

def _p(text):
    return MagicMock(text=text)


def _row(*cell_texts):
    return MagicMock(cells=[MagicMock(text=t) for t in cell_texts])


def _table(*rows):
    return MagicMock(rows=list(rows))


def test_docx_concatenates_paragraphs_and_tables():
    doc = MagicMock()
    doc.paragraphs = [_p("Intro paragraph"), _p(""), _p("Body line")]
    doc.tables = [_table(_row("Action", "Owner"), _row("Validate Q2", "Alice"))]
    _STUBS["docx"].Document.return_value = doc
    out = MOD.extract_text(b"PK...", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "Intro paragraph" in out
    assert "Body line" in out
    assert "Action | Owner" in out
    assert "Validate Q2 | Alice" in out


def test_docx_skips_empty_paragraphs():
    doc = MagicMock()
    doc.paragraphs = [_p(""), _p("only"), _p("")]
    doc.tables = []
    _STUBS["docx"].Document.return_value = doc
    assert MOD.extract_text(b"PK...", "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == "only"


def test_docx_corrupted_returns_empty():
    _STUBS["docx"].Document.side_effect = ValueError("not a docx zip")
    assert MOD.extract_text(b"corrupt", "application/vnd.openxmlformats-officedocument.wordprocessingml.document") == ""


# ─── ODT / ODP (odfpy) ──────────────────────────────────────────

def test_odt_extracts_paragraphs_and_headings():
    def _by_type(t):
        if t is _STUBS["odf_text"].P:
            return ["P-node-1", "P-node-2"]
        if t is _STUBS["odf_text"].H:
            return ["H-node-1"]
        if t is _STUBS["odf_draw"].TextBox:
            return []
        return []

    doc = MagicMock()
    doc.getElementsByType.side_effect = _by_type
    _STUBS["odf_opendocument"].load.return_value = doc
    _STUBS["odf_teletype"].extractText.side_effect = lambda n: {
        "P-node-1": "First paragraph",
        "P-node-2": "Second paragraph",
        "H-node-1": "Section heading",
    }[n]

    out = MOD.extract_text(b"PK...", "application/vnd.oasis.opendocument.text")
    assert "First paragraph" in out
    assert "Second paragraph" in out
    assert "Section heading" in out


def test_odp_includes_slide_text_boxes_without_duplication():
    def _by_type(t):
        if t is _STUBS["odf_text"].P:
            return ["P1"]
        if t is _STUBS["odf_text"].H:
            return []
        if t is _STUBS["odf_draw"].TextBox:
            # Two text-boxes: one duplicates a paragraph, one is unique.
            return ["TB-dup", "TB-unique"]
        return []

    doc = MagicMock()
    doc.getElementsByType.side_effect = _by_type
    _STUBS["odf_opendocument"].load.return_value = doc
    _STUBS["odf_teletype"].extractText.side_effect = lambda n: {
        "P1": "Slide bullet",
        "TB-dup": "Slide bullet",         # already collected via P
        "TB-unique": "Slide title bar",
    }[n]

    out = MOD.extract_text(b"PK...", "application/vnd.oasis.opendocument.presentation")
    # The duplicate must not appear twice.
    assert out.count("Slide bullet") == 1
    assert "Slide title bar" in out


def test_odf_corrupted_returns_empty():
    _STUBS["odf_opendocument"].load.side_effect = Exception("not an odf zip")
    assert MOD.extract_text(b"corrupt", "application/vnd.oasis.opendocument.text") == ""


# ─── PPTX ───────────────────────────────────────────────────────

def _shape(text_lines, with_notes=None):
    paragraphs = []
    for line in text_lines:
        runs = [MagicMock(text=line)]
        paragraphs.append(MagicMock(runs=runs))
    shape = MagicMock()
    shape.has_text_frame = True
    shape.text_frame.paragraphs = paragraphs
    return shape


def _slide(shapes, notes_text=""):
    slide = MagicMock()
    slide.shapes = shapes
    if notes_text:
        slide.notes_slide.notes_text_frame.text = notes_text
    else:
        slide.notes_slide = None
    return slide


def test_pptx_concatenates_shapes_and_notes():
    s1 = _slide([_shape(["Title slide"]), _shape(["Bullet one", "Bullet two"])])
    s2 = _slide([_shape(["Second slide"])], notes_text="Speaker notes for slide 2")
    prs = MagicMock()
    prs.slides = [s1, s2]
    _STUBS["pptx"].Presentation.return_value = prs

    out = MOD.extract_text(b"PK...", "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert "Title slide" in out
    assert "Bullet one" in out
    assert "Bullet two" in out
    assert "Second slide" in out
    assert "Speaker notes for slide 2" in out


def test_pptx_skips_shapes_without_text_frame():
    img_shape = MagicMock()
    img_shape.has_text_frame = False
    img_shape.text_frame.paragraphs = []  # would raise if accessed without the guard
    text_shape = _shape(["Real text"])
    prs = MagicMock()
    prs.slides = [_slide([img_shape, text_shape])]
    _STUBS["pptx"].Presentation.return_value = prs

    assert MOD.extract_text(b"PK...", "application/vnd.openxmlformats-officedocument.presentationml.presentation") == "Real text"


def test_pptx_corrupted_returns_empty():
    _STUBS["pptx"].Presentation.side_effect = Exception("not a pptx zip")
    assert MOD.extract_text(b"corrupt", "application/vnd.openxmlformats-officedocument.presentationml.presentation") == ""


# ─── Plain text / Markdown ──────────────────────────────────────

def test_text_plain_is_passed_through():
    assert MOD.extract_text(b"hello world", "text/plain") == "hello world"


def test_markdown_is_passed_through():
    assert MOD.extract_text(b"# Title\n\nBody", "text/markdown") == "# Title\n\nBody"


def test_text_with_invalid_utf8_falls_back_to_latin1():
    # 0xff is illegal as a UTF-8 lead byte; latin-1 decodes it to U+00FF.
    out = MOD.extract_text(b"caf\xe9 \xff", "text/plain")
    assert "café" in out
    assert "ÿ" in out  # latin-1 decoding of 0xff, with errors=replace not needed here


# ─── Truncation ─────────────────────────────────────────────────

def test_max_chars_truncates_with_marker():
    long_para = "x" * 10_000
    _STUBS["docx"].Document.return_value = MagicMock(paragraphs=[_p(long_para)], tables=[])
    out = MOD.extract_text(
        b"PK...",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        max_chars=200,
    )
    assert len(out) <= 200 + len("\n[…truncated]")
    assert out.endswith("[…truncated]")


def test_max_chars_below_threshold_is_unchanged():
    _STUBS["docx"].Document.return_value = MagicMock(paragraphs=[_p("short")], tables=[])
    out = MOD.extract_text(
        b"PK...",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        max_chars=1000,
    )
    assert out == "short"


# ─── Normalization ──────────────────────────────────────────────

def test_runs_of_blank_lines_collapsed():
    doc = MagicMock()
    doc.paragraphs = [_p("A"), _p(""), _p(""), _p(""), _p("B")]
    doc.tables = []
    _STUBS["docx"].Document.return_value = doc
    out = MOD.extract_text(b"PK...", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    # At most one blank line between A and B.
    assert "A\n\nB" == out or "A\nB" == out


def test_trailing_whitespace_stripped_per_line_and_overall():
    assert MOD.extract_text(b"  hello world   \n\n   ", "text/plain") == "hello world"
