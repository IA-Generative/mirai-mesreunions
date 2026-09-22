"""Tests des helpers de rendu pour le versement Drive (§9bis du plan).

Fonctions pures de rendu markdown et composition du lot de fichiers ;
l'orchestration du versement est couverte par
``test_drive_brief_sync_pipeline``.
"""

import importlib.util
import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PATH = os.path.join(ROOT, "services", "mesreunions-web", "app", "drive_brief_sync.py")


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


def test_build_payload_omits_the_prompt_file_when_the_prompt_is_absent():
    """Au réessai, `used_prompt` vaut None (il n'est pas persisté) : émettre
    un « (non disponible) » écraserait un prompt-utilise.txt valide."""
    out = dbs._build_brief_files_payload({"subject": "x"}, [], None)
    assert "prompt-utilise.txt" not in out
    assert {"brief.md", "glossaire.txt", "documents-source.md"} <= set(out)


def test_build_payload_writes_the_prompt_when_available():
    out = dbs._build_brief_files_payload({"subject": "x"}, [], "PROMPT SUBSTITUÉ")
    assert out["prompt-utilise.txt"] == "PROMPT SUBSTITUÉ".encode("utf-8")


def test_build_payload_truncates_a_very_long_prompt():
    out = dbs._build_brief_files_payload({"subject": "x"}, [], "a" * 60_000)
    assert out["prompt-utilise.txt"].endswith(b"[tronqu\xc3\xa9]")
    assert len(out["prompt-utilise.txt"]) < 60_000


def test_schedule_drive_brief_sync_does_not_raise_on_failure(monkeypatch):
    """Garantit que le thread daemon n'expose aucune erreur au caller.

    La préparation est déjà persistée quand ce thread démarre : tout ce qui
    remonterait d'ici transformerait un export raté en échec de sauvegarde.
    """
    def boom(*_a, **_kw):
        raise RuntimeError("Drive injoignable")

    monkeypatch.setattr(dbs, "_do_sync", boom)
    monkeypatch.setattr(dbs, "_report_status", lambda *_a, **_kw: None)

    # Doit retourner None sans exception.
    assert dbs.schedule_drive_brief_sync(
        user_sub="user-a",
        brief_id="brief-1",
        brief_json={"subject": "x"},
        documents=[],
    ) is None
    # Le worker tourne en daemon ; ses erreurs sont loguées, pas levées.
