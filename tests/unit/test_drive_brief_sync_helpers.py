"""Tests des helpers de rendu pour le versement Drive (§9bis du plan).

Le pipeline Drive effectif (_do_sync) raise NotImplementedError ; on teste
ici uniquement les fonctions pures de rendu markdown.
"""

import importlib.util
import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PATH = os.path.join(ROOT, "services", "code-generator", "app", "drive_brief_sync.py")


def _load():
    spec = importlib.util.spec_from_file_location("dbs_under_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dbs = _load()


def test_brief_json_to_markdown_renders_known_sections():
    brief = {
        "subject": "COPIL DTNUM",
        "objective_reformulated": "Aligner la roadmap",
        "context_summary": "Sprint 12",
        "agenda": [
            {"title": "Budget", "duration_minutes": 30,
             "key_questions": ["Q3 ?", "Q4 ?"]},
        ],
        "open_threads": [{"summary": "Pas d'arbitrage déco"}],
        "participants_notes": [{"name": "Jean", "role": "PM"}],
        "risks": ["Délai serré"],
        "preparation_checklist": ["Lire le doc"],
    }
    md = dbs.brief_json_to_markdown(brief)
    assert "# COPIL DTNUM" in md
    assert "## Objectif reformulé" in md
    assert "## Agenda" in md
    assert "**Budget**" in md
    assert "30 min" in md
    assert "## Risques" in md
    assert "- [ ] Lire le doc" in md


def test_brief_json_to_markdown_handles_empty_input():
    assert dbs.brief_json_to_markdown(None) == ""
    assert dbs.brief_json_to_markdown({}) == "\n"


def test_documents_source_to_markdown_lists_docs_and_totals():
    docs = [
        {"name": "spec.pdf", "id": "abc", "status": "ingested", "chars": 1200},
        {"name": "notes.docx", "id": "def", "status": "ingested", "chars": 800},
    ]
    md = dbs.documents_source_to_markdown(docs, drive_explorer_base="https://drive.example")
    assert "spec.pdf" in md
    assert "notes.docx" in md
    assert "2000 caractères" in md
    assert "https://drive.example/items/abc" in md


def test_schedule_drive_brief_sync_does_not_raise_on_failure():
    """Garantit que le thread daemon ne propage pas son NotImplementedError."""
    # Doit retourner None sans exception.
    dbs.schedule_drive_brief_sync(
        user_sub="user-a",
        brief_id="brief-1",
        brief_json={"subject": "x"},
        documents=[],
    )
    # Le worker tourne en daemon ; ses erreurs sont loguées, pas levées.
