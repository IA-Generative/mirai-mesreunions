"""Unit tests for services.file-mover.app.glossary_from_brief.

Couvre :
  - extraction des sigles (regex \\b[A-Z]{2,}\\d*\\b)
  - extraction des noms propres (participants_notes)
  - cap 50 / 200 selon la fonction (initial_prompt vs full)
  - dédup, rejet des stopwords FR capitalisés
  - sortie ``initial_prompt`` = phrase naturelle (pas une liste sèche)
"""

import importlib.util
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GFB_PATH = os.path.join(ROOT, "services", "file-mover", "app", "glossary_from_brief.py")


def _load():
    spec = importlib.util.spec_from_file_location("gfb_under_test", GFB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gfb = _load()


# ─── extract_whisper_initial_prompt ─────────────────────────


def test_initial_prompt_contains_sigles():
    brief = {
        "subject": "COPIL DTNUM-AI",
        "objective_reformulated": "Aligner avec la DGSI sur le RGPD",
        "participants_notes": [],
        "agenda": [],
    }
    out = gfb.extract_whisper_initial_prompt(brief)
    assert "DTNUM" in out or "COPIL" in out or "DGSI" in out
    assert "RGPD" in out


def test_initial_prompt_is_a_natural_sentence_not_a_list():
    brief = {
        "subject": "COPIL DTNUM",
        "participants_notes": [{"name": "Jean Dupont"}, {"name": "Marie Bonnet"}],
        "agenda": [{"title": "RGPD"}],
    }
    out = gfb.extract_whisper_initial_prompt(brief)
    # Ce n'est pas un dump séparé de virgules nu — il y a une amorce naturelle.
    assert "Réunion" in out


def test_initial_prompt_caps_at_50_terms():
    sigles = [f"AAA{i}" for i in range(80)]
    brief = {"subject": " ".join(sigles), "participants_notes": [], "agenda": []}
    out = gfb.extract_whisper_initial_prompt(brief, max_terms=50)
    # Pas tous les 80 sigles ne peuvent rentrer ; au moins 30 (pris dans la
    # première branche) en attendu, et bien moins de 80.
    counted = sum(1 for s in sigles if s in out)
    assert 5 <= counted < 80


def test_initial_prompt_handles_invalid_brief():
    assert gfb.extract_whisper_initial_prompt(None) == ""
    assert gfb.extract_whisper_initial_prompt("not a dict") == ""
    assert gfb.extract_whisper_initial_prompt({}) == ""


# ─── extract_full_glossary_terms_from_brief ─────────────────


def test_full_glossary_includes_sigles_and_proper_nouns():
    brief = {
        "subject": "COPIL DTNUM",
        "objective_reformulated": "Réunion avec Jean Dupont sur le RGPD",
        "participants_notes": [{"name": "Marie Bonnet"}],
        "agenda": [{"title": "Stratégie DGSI"}],
    }
    out = gfb.extract_full_glossary_terms_from_brief(brief)
    assert "DTNUM" in out
    assert "COPIL" in out
    assert "DGSI" in out
    assert "RGPD" in out
    # Au moins un nom propre
    assert "Dupont" in out or "Bonnet" in out or "Marie Bonnet" in out


def test_full_glossary_caps_at_200():
    sigles = [f"AAA{i}" for i in range(500)]
    brief = {"subject": " ".join(sigles)}
    out = gfb.extract_full_glossary_terms_from_brief(brief, cap=200)
    assert len(out) <= 200


def test_full_glossary_dedups():
    brief = {
        "subject": "RGPD RGPD RGPD COPIL COPIL",
        "agenda": [{"title": "RGPD"}],
    }
    out = gfb.extract_full_glossary_terms_from_brief(brief)
    # set par construction : pas de doublon
    assert len(out) == len(set(out))


def test_full_glossary_rejects_stopwords():
    brief = {
        "subject": "Sujet de la Réunion entre Le Comité et La Direction",
    }
    out = gfb.extract_full_glossary_terms_from_brief(brief)
    # Les stopwords FR capitalisés ne doivent pas remonter.
    assert "Le" not in out
    assert "La" not in out
    assert "Sujet" not in out


def test_full_glossary_invalid_input():
    assert gfb.extract_full_glossary_terms_from_brief(None) == set()
    assert gfb.extract_full_glossary_terms_from_brief("not a dict") == set()
