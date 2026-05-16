"""Unit tests pour le chaînage série + injection {PRIOR_KEY_POINTS}.

Couvre :
  - build_prompt substitue {PRIOR_KEY_POINTS} avec la chaîne fournie.
  - Si vide → "(aucun)".
  - Les 5 templates contiennent bien le placeholder.
"""

import importlib.util
import os
import sys

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _load_meeting_prep():
    """Importe meeting_prep côté mydevices-web."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    path = os.path.join(ROOT, "services", "mydevices-web", "app", "meeting_prep.py")
    spec = importlib.util.spec_from_file_location("mp_series_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mp = _load_meeting_prep()


def test_build_prompt_substitutes_prior_key_points():
    tpl = "Avant la réunion : {PRIOR_KEY_POINTS}\n\nDocs : {PREP_DOCS}\n"
    # Inject les autres placeholders requis pour ne pas planter en KeyError.
    tpl = (
        "Obj: {OBJECTIVE} / Dur: {DURATION_MINUTES} / Rôle: {ROLE_VIEWPOINT} / "
        "Att: {EXPECTATION} / Foc: {FOCUS_AREAS} / Prior: {PRIOR_MEETINGS}\n"
    ) + tpl
    out = mp.build_prompt(
        tpl,
        objective="o", duration_minutes=30, role_viewpoint="r",
        expectation="e", focus_areas=[], prep_docs_text="docs",
        prior_key_points_text="- Décision X validée\n- Action Y en cours",
    )
    assert "Décision X validée" in out
    assert "Action Y en cours" in out


def test_build_prompt_renders_aucun_when_prior_key_points_empty():
    tpl = (
        "Obj: {OBJECTIVE} / Dur: {DURATION_MINUTES} / Rôle: {ROLE_VIEWPOINT} / "
        "Att: {EXPECTATION} / Foc: {FOCUS_AREAS} / Prior: {PRIOR_MEETINGS}\n"
        "Avant : {PRIOR_KEY_POINTS}\nDocs : {PREP_DOCS}\n"
    )
    out = mp.build_prompt(
        tpl,
        objective="o", duration_minutes=30, role_viewpoint="r",
        expectation="e", focus_areas=[], prep_docs_text="docs",
        prior_key_points_text="",
    )
    assert "(aucun)" in out


def test_all_5_prompt_templates_have_prior_key_points_placeholder():
    prompts_dir = os.path.join(
        ROOT, "services", "mydevices-web", "app", "prompts",
    )
    for fname in os.listdir(prompts_dir):
        if not fname.endswith(".txt"):
            continue
        with open(os.path.join(prompts_dir, fname), "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "{PRIOR_KEY_POINTS}" in content, f"{fname} missing placeholder"
