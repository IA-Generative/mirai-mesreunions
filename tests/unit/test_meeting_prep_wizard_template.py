"""
Garde-fou pour le wizard meeting-prep (PREP_BRIEF_TEMPLATE) dans
services/code-generator/app/main.py.

On vérifie via AST extraction (pas d'import de l'app Flask) :

  - le champ ``drive_folder`` n'est plus marqué ``required`` côté HTML,
  - le select ``meeting_type`` est présent avec les 5 options attendues,
  - le bouton « Tester l'accès » (id="test-drive-btn") existe,
  - le bouton « Ouvrir mes fichiers » (id="open-drive-btn") existe,
  - le JS soumet bien ``meeting_type`` au backend,
  - le JS ne bloque plus la soumission sur ``drive_folder`` vide.
"""

import ast
import os
import re

import pytest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAIN_PY = os.path.join(ROOT, "services", "code-generator", "app", "main.py")


def _extract_prep_brief_template() -> str:
    """Renvoie la valeur Python-évaluée de la constante PREP_BRIEF_TEMPLATE."""
    src = open(MAIN_PY, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "PREP_BRIEF_TEMPLATE"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError("PREP_BRIEF_TEMPLATE = '...' introuvable au top-level de main.py")


@pytest.fixture(scope="module")
def tpl() -> str:
    return _extract_prep_brief_template()


def test_template_extractable(tpl):
    assert isinstance(tpl, str)
    assert len(tpl) > 5_000


def test_drive_folder_no_longer_required(tpl):
    """Le champ doit exister sans attribut ``required``."""
    # Extrait le bloc <input ... id="drive_folder" ...>
    m = re.search(r'<input[^>]*id="drive_folder"[^>]*>', tpl)
    assert m, "input drive_folder absent du template"
    assert "required" not in m.group(0), (
        f"L'input drive_folder ne doit plus avoir l'attribut `required` : {m.group(0)}"
    )


def test_drive_folder_label_no_asterisk(tpl):
    """Le label « Dossier Drive » ne doit plus comporter l'astérisque obligatoire."""
    m = re.search(r'<label for="drive_folder">([^<]+)</label>', tpl)
    assert m, "label de drive_folder absent"
    assert "*" not in m.group(1), f"Le label ne doit plus avoir d'astérisque : {m.group(1)!r}"


def test_meeting_type_select_present_with_5_options(tpl):
    assert 'id="meeting_type"' in tpl
    for value in ("general", "one_on_one", "project_update", "steering_committee", "brainstorm"):
        assert f'value="{value}"' in tpl, f"Option meeting_type manquante : {value}"


def test_open_drive_button_present(tpl):
    assert 'id="open-drive-btn"' in tpl
    # Le href est rendu par Jinja → présence du token Jinja attendu
    assert "drive_base_url" in tpl


def test_test_drive_button_present(tpl):
    assert 'id="test-drive-btn"' in tpl
    assert "/api/meeting-prep/test-drive" in tpl


def test_js_submits_meeting_type(tpl):
    """Le JS du form doit envoyer ``meeting_type`` dans le payload."""
    assert "meeting_type:" in tpl or "meeting_type :" in tpl
    assert "meetingType" in tpl  # variable JS dérivée du select


def test_js_does_not_block_on_empty_drive_folder(tpl):
    """Le check des champs obligatoires côté JS ne doit plus inclure folder."""
    # Le check historique était `if (!subject || !folder || !role || !expectation)`.
    # On exige qu'il n'inclue plus la variable `folder` dans la garde.
    m = re.search(r"if \(!subject [^)]*\)", tpl)
    assert m, "garde de validation côté JS introuvable"
    assert "!folder" not in m.group(0), (
        f"La validation JS bloque encore sur le folder : {m.group(0)}"
    )
