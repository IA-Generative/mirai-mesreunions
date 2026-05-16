"""
Unit tests for services.mydevices-web.app.meeting_prep.

Same path-based loading pattern as test_drive_client / test_doc_extractor:
the service package contains a hyphen so it can't be imported with normal
``import`` syntax. We load the module via ``importlib.util.spec_from_file_location``
and exercise the pure helpers (no Drive / no LLM HTTP calls):

  - extract_folder_id : URL → id, raw id passthrough, invalid → None
  - build_prompt      : substitution of every placeholder declared in the
                        template + sentinel for empty / missing inputs
  - load_prompt_template : the shipped conductor_brief.txt actually contains
                           every placeholder the builder references
  - assemble_corpus   : list_children / download_item are mocked; we verify
                        the per-doc cap, total-char cap, doc-count cap and
                        the "used" status surfaced to the UI
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest


# ─── Locate + load the module under test ──────────────────────────

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULE_PATH = os.path.join(ROOT, "services", "mydevices-web", "app", "meeting_prep.py")


def _fresh_meeting_prep(monkeypatch=None):
    """
    Load meeting_prep.py with optional doc_extractor stubbed out so tests do
    not need pypdf / python-docx installed. We do this by pre-registering a
    stub module under the alias meeting_prep uses internally
    ('meeting_prep_doc_extractor'), then exec'ing meeting_prep so it picks
    up our stub when it tries to load doc_extractor.

    Returns the loaded meeting_prep module.
    """
    # Pre-register stubs so the module's _load_module() picks them up.
    # The real drive_client and llm_client only import the stdlib + requests,
    # so they load fine without monkeypatching.
    spec = importlib.util.spec_from_file_location("meeting_prep_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── extract_folder_id ────────────────────────────────────────────

def test_extract_folder_id_full_url():
    mp = _fresh_meeting_prep()
    url = "https://mesfichiers.fake.example/explorer/items/abc-123-def"
    assert mp.extract_folder_id(url) == "abc-123-def"


def test_extract_folder_id_url_with_query():
    mp = _fresh_meeting_prep()
    url = "https://mesfichiers.fake.example/items/abc-123/?foo=bar"
    assert mp.extract_folder_id(url) == "abc-123"


def test_extract_folder_id_url_with_trailing_segment():
    mp = _fresh_meeting_prep()
    url = "https://mesfichiers.fake.example/folders/xyz/edit"
    assert mp.extract_folder_id(url) == "xyz"


def test_extract_folder_id_raw_id():
    mp = _fresh_meeting_prep()
    assert mp.extract_folder_id("abc-123-def") == "abc-123-def"


def test_extract_folder_id_raw_id_with_whitespace():
    mp = _fresh_meeting_prep()
    assert mp.extract_folder_id("  abc-123  ") == "abc-123"


def test_extract_folder_id_empty_returns_none():
    mp = _fresh_meeting_prep()
    assert mp.extract_folder_id("") is None
    assert mp.extract_folder_id("   ") is None
    assert mp.extract_folder_id(None) is None


def test_extract_folder_id_rejects_path_without_items_marker():
    mp = _fresh_meeting_prep()
    # A bare path without /items/ or /folders/ marker is rejected — we don't
    # want to silently treat a malformed URL as a Drive id.
    assert mp.extract_folder_id("foo/bar") is None


# ─── build_prompt ─────────────────────────────────────────────────

_TEMPLATE = (
    "Sujet : {OBJECTIVE}\n"
    "Durée : {DURATION_MINUTES}\n"
    "Rôle : {ROLE_VIEWPOINT}\n"
    "Attente : {EXPECTATION}\n"
    "Focus : {FOCUS_AREAS}\n"
    "Docs : {PREP_DOCS}\n"
    "CRs : {PRIOR_MEETINGS}\n"
)


def test_build_prompt_substitutes_all_placeholders():
    mp = _fresh_meeting_prep()
    out = mp.build_prompt(
        _TEMPLATE,
        objective="Décider du budget Q3",
        duration_minutes=60,
        role_viewpoint="J'anime",
        expectation="Préparer ma prise de parole",
        focus_areas=["Aspects budgétaires", "Risques"],
        prep_docs_text="Note préparatoire…",
        prior_meetings_text="CR du 12 mars…",
    )
    assert "{" not in out  # No placeholder leaked.
    assert "Sujet : Décider du budget Q3" in out
    assert "Durée : 60" in out
    assert "Rôle : J'anime" in out
    assert "Attente : Préparer ma prise de parole" in out
    assert "Focus : Aspects budgétaires, Risques" in out
    assert "Docs : Note préparatoire…" in out
    assert "CRs : CR du 12 mars…" in out


def test_build_prompt_renders_empty_focus_as_sentinel():
    """An empty focus list must not leave the {FOCUS_AREAS} unfilled."""
    mp = _fresh_meeting_prep()
    out = mp.build_prompt(
        _TEMPLATE,
        objective="x",
        duration_minutes=30,
        role_viewpoint="x",
        expectation="x",
        focus_areas=[],
        prep_docs_text="x",
    )
    assert "Focus : (aucun)" in out


def test_build_prompt_handles_missing_prior_meetings():
    mp = _fresh_meeting_prep()
    out = mp.build_prompt(
        _TEMPLATE,
        objective="x",
        duration_minutes=30,
        role_viewpoint="x",
        expectation="x",
        focus_areas=["a"],
        prep_docs_text="x",
    )
    assert "CRs : (aucun)" in out


def test_build_prompt_renders_sentinel_for_blank_strings():
    """The LLM must not see a blank placeholder — caller protected against it."""
    mp = _fresh_meeting_prep()
    out = mp.build_prompt(
        _TEMPLATE,
        objective="   ",
        duration_minutes=30,
        role_viewpoint="",
        expectation="  ",
        focus_areas=["a"],
        prep_docs_text="",
    )
    assert "Sujet : (non précisé)" in out
    assert "Rôle : (non précisé)" in out
    assert "Attente : (non précisée)" in out
    assert "Docs : (aucun document fourni)" in out


def test_build_prompt_strips_focus_blanks():
    """Empty / whitespace-only focus entries are dropped."""
    mp = _fresh_meeting_prep()
    out = mp.build_prompt(
        _TEMPLATE,
        objective="x",
        duration_minutes=30,
        role_viewpoint="x",
        expectation="x",
        focus_areas=["", "  ", "Risques"],
        prep_docs_text="x",
    )
    assert "Focus : Risques" in out


# ─── load_prompt_template ─────────────────────────────────────────

def test_load_prompt_template_finds_all_required_placeholders():
    """The shipped conductor_brief.txt must declare every placeholder the
    builder will try to fill — otherwise the substitution silently leaves
    {X} in the prompt that goes to the LLM."""
    mp = _fresh_meeting_prep()
    text = mp.load_prompt_template()
    for placeholder in mp._REQUIRED_PLACEHOLDERS:
        assert placeholder in text, f"Missing placeholder in template: {placeholder}"


def test_load_prompt_template_raises_on_missing_placeholder(tmp_path):
    mp = _fresh_meeting_prep()
    bad = tmp_path / "bad.txt"
    bad.write_text("only has {OBJECTIVE} and nothing else", encoding="utf-8")
    with pytest.raises(RuntimeError, match="missing placeholders"):
        mp.load_prompt_template(str(bad))


# ─── assemble_corpus ──────────────────────────────────────────────

def _fake_drive(children, downloads):
    """
    Build a MagicMock standing in for DriveClient.

    ``downloads`` maps item_id → (bytes, content_type). Items not in this
    map raise DriveApplicativeError so we can also exercise the "skip the
    broken doc but keep going" branch.
    """
    drive = MagicMock()
    drive.list_children.return_value = children

    def _download(_access_token, item_id, max_bytes=None):
        if item_id not in downloads:
            from importlib import import_module  # only to grab the exception
            raise mp_module_for_exc.DriveApplicativeError(f"missing {item_id}")
        body, ctype = downloads[item_id]
        if max_bytes is not None and len(body) > max_bytes:
            raise mp_module_for_exc.DriveApplicativeError(f"too big {item_id}")
        return body, ctype

    drive.download_item.side_effect = _download
    return drive


# Module-level handle so the _download closure above can reach the loaded
# DriveApplicativeError class without re-loading meeting_prep on every call.
mp_module_for_exc = _fresh_meeting_prep()


def test_assemble_corpus_concatenates_and_records_used():
    mp = mp_module_for_exc
    children = [
        {"id": "1", "title": "Note de cadrage.txt"},
        {"id": "2", "title": "Diagnostic.md"},
    ]
    downloads = {
        "1": (b"Cadrage : le sujet est le budget Q3.", "text/plain"),
        "2": (b"# Diagnostic\n\nLes risques principaux sont X et Y.", "text/markdown"),
    }
    drive = _fake_drive(children, downloads)
    corpus, used = mp.assemble_corpus(drive, "ACCESS", "FOLDER_ID")

    assert "--- Note de cadrage.txt ---" in corpus
    assert "Cadrage : le sujet est le budget Q3." in corpus
    assert "--- Diagnostic.md ---" in corpus
    assert "Les risques principaux sont X et Y." in corpus
    statuses = {u["name"]: u["status"] for u in used}
    assert statuses["Note de cadrage.txt"] == "ingested"
    assert statuses["Diagnostic.md"] == "ingested"


def test_assemble_corpus_skips_folders():
    mp = mp_module_for_exc
    children = [
        {"id": "f1", "title": "Sous-dossier", "type": "folder"},
        {"id": "1", "title": "Note.txt"},
    ]
    drive = _fake_drive(children, {"1": (b"contenu", "text/plain")})
    corpus, used = mp.assemble_corpus(drive, "ACCESS", "FOLDER_ID")

    statuses = {u["name"]: u["status"] for u in used}
    assert statuses["Sous-dossier"] == "skipped_folder"
    assert statuses["Note.txt"] == "ingested"
    # Folder should NOT have triggered a download_item call.
    called_ids = [c.kwargs.get("item_id") or c.args[1] for c in drive.download_item.call_args_list]
    assert "f1" not in called_ids


def test_assemble_corpus_respects_per_doc_cap():
    mp = mp_module_for_exc
    big = ("x" * 200).encode("utf-8")
    drive = _fake_drive(
        [{"id": "1", "title": "Big.txt"}],
        {"1": (big, "text/plain")},
    )
    corpus, used = mp.assemble_corpus(
        drive, "ACCESS", "FOLDER_ID",
        per_doc_max_chars=50, total_max_chars=10_000, max_docs=10,
    )
    # 50 chars from doc_extractor's max_chars (which appends "[…truncated]").
    ingested = next(u for u in used if u["name"] == "Big.txt")
    assert ingested["status"] == "ingested"
    assert ingested["chars"] <= 70  # 50 + a few extra for truncation suffix


def test_assemble_corpus_respects_doc_count_cap():
    mp = mp_module_for_exc
    children = [{"id": str(i), "title": f"D{i}.txt"} for i in range(5)]
    downloads = {str(i): (f"doc {i}".encode("utf-8"), "text/plain") for i in range(5)}
    drive = _fake_drive(children, downloads)
    corpus, used = mp.assemble_corpus(
        drive, "ACCESS", "FOLDER_ID",
        max_docs=2, per_doc_max_chars=1000, total_max_chars=10_000,
    )
    statuses = [u["status"] for u in used]
    assert statuses.count("ingested") == 2
    assert statuses.count("skipped_doc_cap") == 3


def test_assemble_corpus_records_download_error_but_continues():
    mp = mp_module_for_exc
    children = [
        {"id": "broken", "title": "Cassé.pdf"},
        {"id": "ok", "title": "OK.txt"},
    ]
    drive = _fake_drive(children, {"ok": (b"hello", "text/plain")})
    corpus, used = mp.assemble_corpus(drive, "ACCESS", "FOLDER_ID")

    statuses = {u["name"]: u["status"] for u in used}
    assert statuses["Cassé.pdf"] == "error_download"
    assert statuses["OK.txt"] == "ingested"
    assert "hello" in corpus


def test_assemble_corpus_propagates_drive_transient():
    """A transient Drive error must bubble — partial corpus would mislead the LLM."""
    mp = mp_module_for_exc
    drive = MagicMock()
    drive.list_children.return_value = [{"id": "1", "title": "Doc.txt"}]
    drive.download_item.side_effect = mp.DriveTransientError("boom")
    with pytest.raises(mp.DriveTransientError):
        mp.assemble_corpus(drive, "ACCESS", "FOLDER_ID")


def test_assemble_corpus_marks_unsupported_documents():
    mp = mp_module_for_exc
    children = [{"id": "1", "title": "Image.png"}]
    drive = _fake_drive(children, {"1": (b"\x89PNG\r\n...", "image/png")})
    corpus, used = mp.assemble_corpus(drive, "ACCESS", "FOLDER_ID")
    assert used[0]["status"] == "skipped_unsupported_or_empty"
    assert corpus == ""
