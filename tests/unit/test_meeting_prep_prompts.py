"""
Tests for the multi-prompt resolution in services.code-generator.app.meeting_prep.

The wizard now picks among 5 prompt files via ``prompt_path_for_type``. These
tests verify that:

  - the 5 prompt files actually ship on disk (so a renamed/missing file
    cannot reach prod silently),
  - ``prompt_path_for_type`` maps each known key to the right file and
    falls back to the general prompt for unknown/empty input,
  - every shipped prompt declares every required placeholder so
    ``load_prompt_template`` accepts it.
"""

import importlib.util
import os

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MODULE_PATH = os.path.join(ROOT, "services", "code-generator", "app", "meeting_prep.py")
PROMPTS_DIR = os.path.join(ROOT, "services", "code-generator", "app", "prompts")


def _load():
    spec = importlib.util.spec_from_file_location("meeting_prep_under_test_prompts", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_prompt_files_exist():
    mp = _load()
    for key, filename in mp.PROMPT_FILES_BY_TYPE.items():
        path = os.path.join(PROMPTS_DIR, filename)
        assert os.path.isfile(path), f"Missing prompt file for type={key}: {path}"


@pytest.mark.parametrize("key,filename", [
    ("general", "conductor_brief.txt"),
    ("one_on_one", "one_on_one.txt"),
    ("project_update", "project_update.txt"),
    ("steering_committee", "steering_committee.txt"),
    ("brainstorm", "brainstorm.txt"),
])
def test_prompt_path_for_type_maps_correctly(key, filename):
    mp = _load()
    path = mp.prompt_path_for_type(key)
    assert os.path.basename(path) == filename
    assert os.path.isfile(path)


def test_prompt_path_for_type_falls_back_to_general_on_empty():
    mp = _load()
    assert os.path.basename(mp.prompt_path_for_type(None)) == "conductor_brief.txt"
    assert os.path.basename(mp.prompt_path_for_type("")) == "conductor_brief.txt"
    assert os.path.basename(mp.prompt_path_for_type("   ")) == "conductor_brief.txt"


def test_prompt_path_for_type_falls_back_to_general_on_unknown_key():
    mp = _load()
    assert os.path.basename(mp.prompt_path_for_type("nope_not_real")) == "conductor_brief.txt"


def test_prompt_path_for_type_is_case_insensitive():
    mp = _load()
    assert os.path.basename(mp.prompt_path_for_type("ONE_ON_ONE")) == "one_on_one.txt"
    assert os.path.basename(mp.prompt_path_for_type("Brainstorm")) == "brainstorm.txt"


@pytest.mark.parametrize("key", [
    "general", "one_on_one", "project_update", "steering_committee", "brainstorm",
])
def test_each_prompt_loads_with_all_placeholders(key):
    """Each shipped prompt must declare every placeholder build_prompt fills."""
    mp = _load()
    text = mp.load_prompt_template(mp.prompt_path_for_type(key))
    for placeholder in mp._REQUIRED_PLACEHOLDERS:
        assert placeholder in text, f"Prompt {key} missing placeholder {placeholder}"


def test_load_prompt_template_default_is_general():
    """Backwards-compat: calling load_prompt_template() with no arg still
    returns the general prompt (no caller passing a path must break)."""
    mp = _load()
    text = mp.load_prompt_template()
    assert "{OBJECTIVE}" in text
    # The general prompt uses the historical "conductor d'une réunion" phrasing.
    assert "assistant de préparation d'une réunion" in text
