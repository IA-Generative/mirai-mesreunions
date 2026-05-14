"""
Garde-fou pour `INDEX_TEMPLATE` (services/code-generator/app/main.py).

INDEX_TEMPLATE est une triple-quoted Python string qui embarque ~3000 lignes
d'HTML + JS rendues par render_template_string. Les escape sequences `\\n`,
`\\'` etc. dans le JS embarqué sont interprétées par Python à l'import — il
faut écrire `\\\\n` dans le source pour qu'un `\\n` JS littéral survive. Cette
classe de bug a cassé silencieusement le parser JS du browser trois fois
pendant le sprint UX mydevices (May 2026).

Les checks ici :
  1. La string INDEX_TEMPLATE existe et n'a pas de double-escape rate
     (présence de `\\\\n` à 4 backslashes = bug).
  2. Le rendu Jinja minimal n'explose pas et contient les motifs attendus.
  3. Le contenu du <script> principal parse via `node --check` (skip si
     node absent — par ex. en CI sans toolchain JS).
  4. Le tool tools/preview_mydevices.py extrait bien le template via AST
     (et non regex sur le source brut, qui laissait les backslashes doubles).
"""

import ast
import os
import re
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

MAIN_PY = os.path.join(ROOT, "services", "code-generator", "app", "main.py")


def _extract_index_template() -> str:
    """Renvoie la valeur Python-évaluée de la constante INDEX_TEMPLATE."""
    src = open(MAIN_PY, "r", encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "INDEX_TEMPLATE"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise RuntimeError("INDEX_TEMPLATE = '...' introuvable au top-level de main.py")


# ─── 1. La constante existe + pas de double-escape rate ────────────────────

def test_index_template_constant_exists():
    tpl = _extract_index_template()
    assert isinstance(tpl, str)
    assert len(tpl) > 10_000, "INDEX_TEMPLATE devrait peser plusieurs Ko"


def test_no_double_escaped_newline_in_template():
    """`\\\\n` (4 backslashes) dans le JS rendu = bug Python-escape raté.

    Reproduit le bug observé pendant le sprint : `'l\\\\'analyse'` dans
    le source Python rendait `l\\\\'analyse` dans le HTML au lieu de
    `l\\'analyse`, ce qui cassait le parser JS sur "Unexpected identifier
    'analyse'". Le pattern à bannir est donc la séquence 4-backslashes-
    suivi-de-n ou 4-backslashes-suivi-d'une-apostrophe dans la string
    Python-évaluée.
    """
    tpl = _extract_index_template()
    assert "\\\\n" not in tpl, (
        "INDEX_TEMPLATE contient `\\\\n` (4 backslashes) — sûrement un "
        "double-escape Python raté. Cherche `\\\\\\\\n` dans le source et "
        "ramène-le à `\\\\n` (deux backslashes Python = un backslash JS)."
    )
    assert "\\\\'" not in tpl, (
        "INDEX_TEMPLATE contient `\\\\'` (3 backslashes + apostrophe) — "
        "double-escape Python raté qui casse le parser JS."
    )


# ─── 2. Rendu Jinja minimal ne lève pas + motifs clés présents ─────────────

@pytest.fixture
def rendered_html():
    """Rend le template avec un contexte minimal et renvoie le HTML."""
    flask = pytest.importorskip("flask")
    tpl = _extract_index_template()
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
        "Préparer une réunion",     # 5e onglet (piste 1 meeting-prep first-class)
        "data-trash-kind=\"brief\"", # corbeille unifiée — briefs aux côtés des fichiers
    ]
    for needle in must_have:
        assert needle in rendered_html, f"Motif attendu absent du rendu : {needle!r}"


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
    """Extrait le premier <script> non-src et le valide via node --check.

    Ce check attrape la classe d'erreurs où une string Python contenant `\\n`
    finit comme un saut de ligne réel dans le JS, brisant un template literal
    ou un commentaire. Vu trois fois pendant le sprint sur des chaînes
    contenant `l\\'analyse` ou `lines.join('\\n')`.
    """
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
            "node --check a échoué — bug d'escape probable dans "
            "INDEX_TEMPLATE.\nSTDERR :\n" + (result.stderr or "<vide>")
        )
    finally:
        os.unlink(js_path)


# ─── 4. tools/preview_mydevices.py utilise bien l'AST extractor ────────────

def test_preview_tool_uses_ast_not_regex():
    """Vérifie que le preview tool extrait via AST.

    Une extraction par regex sur le source brut (triple-quoted)
    retournerait les backslashes doubles non-réduits, ce qui re-casserait
    le rendu côté preview. On vérifie deux choses :
      - le module se charge,
      - sa fonction load_template() renvoie une string sans double-escape.
    """
    tools_dir = os.path.join(ROOT, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import importlib
    mod = importlib.import_module("preview_mydevices")
    tpl = mod.load_template()
    assert isinstance(tpl, str)
    assert "\\\\n" not in tpl, (
        "preview_mydevices.load_template() renvoie un template avec des "
        "backslashes doubles non-réduits — sûrement repassé en regex au "
        "lieu d'AST. Re-vérifier `load_template()`."
    )
