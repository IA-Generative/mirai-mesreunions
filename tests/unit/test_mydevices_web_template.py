"""
Garde-fou pour le template mydevices
(`services/mydevices-web/app/templates/index.html`).

Ce template embarque ~3000 lignes d'HTML + JS rendues par render_template.
Avant l'extraction du template hors du source Python, le risque principal
était le double-escape Python (séquences ``\\n``, ``\\'``) qui cassait le
parser JS du browser. Maintenant que le template vit dans un fichier ``.html``
distinct, ce risque a disparu — mais les autres invariants (présence des
ancres UX clés, JS qui parse) restent utiles.

Les checks :
  1. Le fichier template existe et fait plusieurs Ko.
  2. Le rendu Jinja minimal n'explose pas et contient les motifs attendus.
  3. Le contenu du <script> principal parse via ``node --check`` (skip si
     node absent — par ex. en CI sans toolchain JS).
  4. Le tool ``tools/preview_mydevices.py`` charge bien le template.
"""

import os
import re
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

INDEX_TEMPLATE_PATH = os.path.join(
    ROOT, "services", "mydevices-web", "app", "templates", "index.html"
)


def _load_template() -> str:
    return open(INDEX_TEMPLATE_PATH, "r", encoding="utf-8").read()


# ─── 1. Le template existe et a une taille raisonnable ─────────────────────

def test_index_template_file_exists():
    assert os.path.isfile(INDEX_TEMPLATE_PATH), (
        f"Template attendu à {INDEX_TEMPLATE_PATH}"
    )
    tpl = _load_template()
    assert len(tpl) > 10_000, "Le template devrait peser plusieurs Ko"


# ─── 2. Rendu Jinja minimal ne lève pas + motifs clés présents ─────────────

@pytest.fixture
def rendered_html():
    """Rend le template avec un contexte minimal et renvoie le HTML."""
    flask = pytest.importorskip("flask")
    tpl = _load_template()
    app = flask.Flask(__name__)
    with app.test_request_context("/"):
        html = flask.render_template_string(
            tpl,
            user={"name": "Test User", "email": "t@example.com"},
            short_ttl_enabled=False,
            device_retention_days=15,
        )
    return html


def test_render_contains_expected_landmarks(rendered_html):
    """Le rendu doit contenir les ancres UX clés du sprint."""
    must_have = [
        "Mes réunions (IA)",        # onglet principal
        "file-row-dot",             # animation dot
        "downloads-icon-btn",       # icônes formats
        "Mode avancé",              # toggle power-user
        "_buildInfoTooltip",        # checklist du (i)
        "file-detail-source-filename",  # nom de fichier audio bleuté en détail
        "Préparation de réunion",   # 5e onglet (piste 1 meeting-prep first-class)
        "data-trash-kind=\"brief\"", # corbeille unifiée — briefs aux côtés des fichiers
        # Sprint meeting-prep amend UI : éditeur structuré (remplace textarea JSON).
        "data-amend-form",
        "data-add-agenda-item",
        "data-add-participant",
        "data-add-thread",
        "data-add-opening-question",
        "data-add-risk",
        "data-add-checklist-item",
    ]
    for needle in must_have:
        assert needle in rendered_html, f"Motif attendu absent du rendu : {needle!r}"

    # Assertion négative : l'ancienne textarea JSON brut a bien disparu de la
    # fonction qui peuple le pane « Amender ». On vérifie que le pattern
    # exact qui copiait le JSON dans la textarea n'apparaît plus.
    forbidden = "brief-amend-text').value = JSON.stringify(b.brief_json"
    assert forbidden not in rendered_html, (
        "L'ancien comportement (textarea remplie avec JSON.stringify(brief_json)) "
        "est toujours présent — l'éditeur structuré n'a pas remplacé l'éditeur brut."
    )


def test_render_user_name_injected(rendered_html):
    """Vérifie que le contexte Jinja est bien interpolé (smoke check)."""
    assert "Test User" in rendered_html


# ─── 3. Le <script> parse en JS (node --check) ─────────────────────────────

def _have_node():
    try:
        subprocess.run(
            ["node", "--version"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _have_node(), reason="node introuvable (skip JS parse)")
def test_inline_script_parses_via_node(rendered_html):
    """Extrait le premier <script> non-src et le valide via node --check."""
    m = re.search(
        r'<script(?![^>]*src=)[^>]*>(.*?)</script>',
        rendered_html, re.DOTALL,
    )
    assert m is not None, "Aucun bloc <script> inline trouvé dans le rendu"
    js = m.group(1)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8",
    ) as fh:
        fh.write(js)
        js_path = fh.name
    try:
        result = subprocess.run(
            ["node", "--check", js_path],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 0, (
            "node --check a échoué.\nSTDERR :\n" + (result.stderr or "<vide>")
        )
    finally:
        os.unlink(js_path)


# ─── 4. tools/preview_mydevices.py charge bien le template ────────────────

def test_preview_tool_loads_template():
    """Vérifie que le preview tool ouvre bien le fichier template extrait."""
    tools_dir = os.path.join(ROOT, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import importlib
    mod = importlib.import_module("preview_mydevices")
    tpl = mod.load_template()
    assert isinstance(tpl, str)
    assert len(tpl) > 10_000
