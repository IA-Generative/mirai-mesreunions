"""
Garde-fou pour le template mydevices
(`services/mesreunions-web/app/templates/index.html`).

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
    ROOT, "services", "mesreunions-web", "app", "templates", "index.html"
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
    """Rend le template avec un contexte minimal et renvoie le HTML.

    PR4 : index.html `{% include '_vite_shell_loader.html' %}` — on configure
    le template_folder pour que le loader Jinja trouve le partial.
    """
    flask = pytest.importorskip("flask")
    template_dir = os.path.join(
        ROOT, "services", "mesreunions-web", "app", "templates"
    )
    app = flask.Flask(__name__, template_folder=template_dir)
    with app.test_request_context("/"):
        html = flask.render_template(
            "index.html",
            user={"name": "Test User", "email": "t@example.com", "roles": []},
            short_ttl_enabled=False,
            device_retention_days=15,
            allowed_audio_extensions="m4a,mp3,wav",
        )
    return html


def test_render_contains_expected_landmarks(rendered_html):
    """Le rendu doit contenir les ancres UX clés du sprint."""
    must_have = [
        "Mes réunions (IA)",        # onglet principal
        "file-row-dot",             # animation dot (référencée dans CSS)
        "downloads-icon-btn",       # icônes formats (référencée dans CSS)
        "Mode avancé",              # toggle power-user (label visible)
        "file-detail-source-filename",  # nom de fichier audio bleuté (CSS)
        "Préparation de réunion",   # 5e onglet (piste 1 meeting-prep first-class)
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

    # Garantie PR4 : aucune ancienne ancre JS de l'inline ne traîne dans
    # le rendu HTML (le code a été déplacé vers frontend/legacy.js).
    forbidden_inline = [
        "_buildInfoTooltip",   # fonction JS, plus dans le template
        "function loadSessions",
        "function loadDevices",
        "function loadBriefs",
    ]
    for needle in forbidden_inline:
        assert needle not in rendered_html, (
            f"Trace de JS inline encore présente : {needle!r} — "
            "doit vivre dans frontend/legacy.js depuis PR4."
        )


def test_render_user_name_injected(rendered_html):
    """Vérifie que le contexte Jinja est bien interpolé (smoke check)."""
    assert "Test User" in rendered_html


def test_dsfr_assets_self_hosted(rendered_html):
    """Le template doit charger DSFR depuis /static/dsfr/... (auto-hébergé
    via scripts/copy-dsfr-assets.mjs) et plus depuis le CDN jsdelivr.

    Rationale : CSP-friendly (pas de domaine tiers), perf (pas de DNS lookup),
    intégrité prévisible (version figée par package-lock), licence DSFR
    respectée (assets servis depuis le service lui-même)."""
    # Assets locaux présents
    assert "/static/dsfr/dsfr/dsfr.min.css" in rendered_html
    assert "/static/dsfr/dsfr/dsfr.module.min.js" in rendered_html
    # Le CDN jsdelivr ne doit plus être référencé pour DSFR
    assert "cdn.jsdelivr.net/npm/@gouvfr/dsfr" not in rendered_html, (
        "DSFR doit être servi depuis /static/dsfr/ (auto-hébergé), pas depuis "
        "le CDN. Lance `npm run build` ou `npm run build:assets` pour copier "
        "les fichiers depuis node_modules vers app/static/dsfr/."
    )


def test_dsfr_root_attributes_and_footer(rendered_html):
    """Le squelette DSFR exige `data-fr-theme` sur <html> (sinon le scheme
    sombre/clair ne s'applique pas) et un <footer class="fr-footer"> pour
    la conformité minimale au Système de Design de l'État."""
    assert "data-fr-theme" in rendered_html, (
        "L'attribut data-fr-theme doit être présent sur <html> pour activer "
        "le système de scheme DSFR."
    )
    assert 'class="fr-footer"' in rendered_html, (
        "Le footer DSFR (fr-footer) est requis par le Système de Design "
        "de l'État."
    )


def test_dsfr_fr_tabs_nav_structure(rendered_html):
    """Refonte UX onglets : la nav doit utiliser la structure DSFR native
    `fr-tabs` / `fr-tabs__list` / `fr-tabs__panel` (pas la nav custom
    historique `.tabs-nav`)."""
    assert '<div class="fr-tabs">' in rendered_html, (
        "Le conteneur fr-tabs DSFR doit envelopper la nav d'onglets."
    )
    assert 'class="fr-tabs__list"' in rendered_html, (
        "fr-tabs__list (ul) est requis par la structure DSFR fr-tabs."
    )
    # 7 onglets : transfers, brief, devices, generate, useful-data, trash, admin
    expected_tab_ids = [
        "tab-btn-transfers",
        "tab-btn-brief",
        "tab-btn-devices",
        "tab-btn-generate",
        "tab-btn-useful-data",
        "tab-btn-trash",
        "tab-btn-admin",
    ]
    for tid in expected_tab_ids:
        assert f'id="{tid}"' in rendered_html, (
            f"Bouton d'onglet {tid} attendu dans la nav fr-tabs."
        )
    # 7 panneaux correspondants
    expected_panel_ids = [
        "panel-transfers",
        "panel-brief",
        "panel-devices",
        "panel-generate",
        "panel-useful-data",
        "panel-trash",
        "panel-admin",
    ]
    for pid in expected_panel_ids:
        assert f'id="{pid}"' in rendered_html, (
            f"Panneau {pid} attendu (référencé par aria-controls)."
        )
    # Panel transfers doit être pré-sélectionné par défaut.
    assert 'fr-tabs__panel--selected card tab-pane" data-tab="transfers"' in rendered_html


def test_dsfr_admin_tab_hidden_by_default(rendered_html):
    """L'onglet Admin est masqué par défaut dans la nav ; tabs/admin.js le
    révèle au boot si la claim OIDC `admin` est présente. Le test render
    sans rôle admin → le `<li>` doit avoir display:none."""
    assert 'id="tab-btn-admin-li"' in rendered_html
    assert 'id="tab-btn-admin-li" style="display:none;"' in rendered_html


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


def test_no_inline_logic_script_remains(rendered_html):
    """Depuis PR4, tout le JS applicatif vit dans frontend/ (bundlé par
    Vite). Le template ne doit plus contenir que des ``<script type="module"
    src="...">`` (loader Vite + DSFR) et un seul ``<script
    type="application/json" id="bootstrap-data">`` (contexte Flask)."""
    inline_blocks = re.findall(
        r'<script(?![^>]*\bsrc=)(?![^>]*\btype="application/json")[^>]*>(.*?)</script>',
        rendered_html, re.DOTALL,
    )
    # On tolère un éventuel <script> sans src ET non-json si vide / pur
    # commentaire (peu probable), mais surtout pas de logique applicative.
    for blk in inline_blocks:
        stripped = re.sub(r'/\*.*?\*/', '', blk, flags=re.DOTALL)
        stripped = re.sub(r'//.*', '', stripped)
        assert stripped.strip() == "", (
            "Un <script> inline contient encore du JS applicatif :\n"
            + blk[:200]
        )

    # Le bootstrap-data doit être présent avec les clés clés.
    assert 'id="bootstrap-data"' in rendered_html
    assert 'allowed_audio_extensions' in rendered_html
    assert 'device_retention_days' in rendered_html
    # Le loader Vite doit être inclus.
    assert 'dist/shell.js' in rendered_html, (
        "Le bundle Vite (dist/shell.js) n'est pas chargé — vérifier "
        "l'include {% include '_vite_shell_loader.html' %}."
    )


@pytest.mark.skipif(not _have_node(), reason="node introuvable (skip JS parse)")
def test_frontend_shell_parses_via_node():
    """Valide que le bundle source frontend/shell.js parse en JS. Le bundle
    construit (app/static/dist/shell.js) est testé par le build CI."""
    shell_path = os.path.join(
        ROOT, "services", "mesreunions-web", "frontend", "shell.js",
    )
    assert os.path.isfile(shell_path), f"introuvable : {shell_path}"
    # node --check sait parser un .mjs avec imports ES sans les résoudre.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mjs", delete=False, encoding="utf-8",
    ) as fh:
        fh.write(open(shell_path).read())
        mjs_path = fh.name
    try:
        result = subprocess.run(
            ["node", "--check", mjs_path],
            capture_output=True, text=True, timeout=15,
        )
    finally:
        os.unlink(mjs_path)
    assert result.returncode == 0, (
        "node --check a échoué sur frontend/shell.js.\nSTDERR :\n"
        + (result.stderr or "<vide>")
    )


@pytest.mark.skipif(not _have_node(), reason="node introuvable (skip JS parse)")
def test_frontend_legacy_parses_via_node():
    """Valide que frontend/legacy.js (~3300 lignes extraites de l'inline)
    parse correctement après remplacement des interpolations Jinja."""
    legacy_path = os.path.join(
        ROOT, "services", "mesreunions-web", "frontend", "legacy.js",
    )
    assert os.path.isfile(legacy_path), f"introuvable : {legacy_path}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mjs", delete=False, encoding="utf-8",
    ) as fh:
        fh.write(open(legacy_path).read())
        mjs_path = fh.name
    try:
        result = subprocess.run(
            ["node", "--check", mjs_path],
            capture_output=True, text=True, timeout=15,
        )
    finally:
        os.unlink(mjs_path)
    assert result.returncode == 0, (
        "node --check a échoué sur frontend/legacy.js.\nSTDERR :\n"
        + (result.stderr or "<vide>")
    )


# ─── 3bis. Garde-fou : fonctions onclick="" publiées sur window ──────────

# legacy.js est chargé via shell.js en `<script type="module">` (Vite). Les
# fonctions déclarées dans un module ES NE SONT PAS accessibles depuis les
# attributs `onclick="..."` du template (évalués dans le scope global). Sans
# publication explicite `window.<fn> = <fn>`, chaque clic déclenche
# « ReferenceError: <fn> is not defined » silencieusement (bugs UX :
# titre liste, mode avancé, chevron détails, purge corbeille).
# Ce test prévient toute régression en vérifiant que chaque nom apparaissant
# en `onclick="<name>("` (template Jinja + HTML innerHTML dans legacy.js) est
# soit publié sur window dans legacy.js, soit dans un autre module (preparations.js).

_LEGACY_PATH = os.path.join(
    ROOT, "services", "mesreunions-web", "frontend", "legacy.js"
)
_PREPS_PATH = os.path.join(
    ROOT, "services", "mesreunions-web", "frontend", "tabs", "preparations.js"
)

_BUILTIN_INLINE_GLOBALS = {"document", "event", "window", "console"}


def _collect_inline_handler_names():
    names = set()
    pattern = re.compile(r'on(?:click|change|input|submit)="([a-zA-Z_][a-zA-Z0-9_]*)\(')
    for path in (INDEX_TEMPLATE_PATH, _LEGACY_PATH):
        try:
            content = open(path, "r", encoding="utf-8").read()
        except FileNotFoundError:
            continue
        for m in pattern.finditer(content):
            n = m.group(1)
            if n not in _BUILTIN_INLINE_GLOBALS:
                names.add(n)
    return names


def _collect_window_published(path):
    src = open(path, "r", encoding="utf-8").read()
    names = set()
    for m in re.finditer(r"\bwindow\.([a-zA-Z_][a-zA-Z0-9_]*)\s*=", src):
        names.add(m.group(1))
    block = re.search(r"_WINDOW_EXPORTS\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    if block:
        for line in block.group(1).splitlines():
            t = line.strip().rstrip(",").split(":")[0].strip()
            if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", t):
                names.add(t)
    return names


# ─── 4. Meeting-prep v2 — selectors des 9 lots (PR-1 à PR-6) ──────────────


def test_meeting_prep_v2_selectors_present(rendered_html):
    """Sprint meeting-prep v2 (9 lots) : tous les nouveaux selectors UI
    doivent être présents dans le template rendu. Cette liste sert de
    spec d'intégration entre les modules ES (preparations.js, wizard.js,
    lib/*.js) et le template Jinja."""
    must_have = [
        # Lot 1+2 — endpoints API exposés côté JS (pas dans le template HTML
        # rendu : les sélecteurs sont créés dynamiquement par wizard.js).
        # On se contente ici des ancres HTML stables.
        # Lot 3a — édition inline du titre
        "brief-detail-title-input",
        "brief-detail-title-edit-controls",
        # Lot 3c — modale glossaire
        "brief-detail-glossary-btn",
        "brief-detail-glossary-count",
        # Lot 3 — bouton link-audio (modale)
        "brief-detail-link-audio-btn",
        # Lot 3 — bouton prepare-next avec date picker
        "brief-detail-prepare-next",
        # Lot 4 — menu export 4 formats
        "brief-detail-export-btn",
        "brief-detail-export-menu",
        'data-export-format="docx"',
        'data-export-format="odt"',
        'data-export-format="txt"',
        'data-export-format="md"',
        # Lot 5 — participants éditables
        "brief-detail-participants",
        "brief-detail-participants-list",
        # Lot 6 — bloc récurrence
        "brief-detail-recurrence",
        "brief-detail-recurring-toggle",
        "brief-detail-recurrence-form",
        "brief-detail-next-occurrence",
        "wizard-recurrence-form",
        # Lot 7 — timeline série
        "brief-detail-series-timeline",
        # Lot 8 — emails (mailto + toggle CR)
        "brief-detail-emails",
        "brief-detail-mailto-link",
        "brief-detail-send-cr-toggle",
        # Lot 9 — thématiques
        "brief-detail-themes",
        "brief-detail-themes-save-btn",
        "wizard-themes-container",
    ]
    missing = [s for s in must_have if s not in rendered_html]
    assert not missing, (
        "Selectors meeting-prep v2 absents du template :\n"
        + "\n".join("  - " + s for s in missing)
    )


def test_meeting_prep_v2_lib_modules_exist():
    """Les helpers frontend lib/ exigés par les 9 lots doivent exister."""
    lib_dir = os.path.join(
        ROOT, "services", "mesreunions-web", "frontend", "lib",
    )
    expected = [
        "rrule-builder.js",        # Lot 6
        "mailto-builder.js",       # Lot 8
        "export-formatter.js",     # Lot 4 (TXT/MD côté front)
        "series-timeline.js",      # Lot 7
        "themes-chips.js",         # Lot 9
        "participants.js",         # Lot 5
        "prep-modal.js",           # Lot 3 (modales link-audio + glossaire)
    ]
    missing = [
        f for f in expected
        if not os.path.isfile(os.path.join(lib_dir, f))
    ]
    assert not missing, (
        "Modules frontend/lib/ manquants : " + ", ".join(missing)
    )


def test_inline_handlers_are_published_on_window():
    referenced = _collect_inline_handler_names()
    published = _collect_window_published(_LEGACY_PATH) | _collect_window_published(_PREPS_PATH)
    missing = referenced - published
    assert not missing, (
        "Ces fonctions sont appelées via onclick/onchange dans le template "
        "ou dans le HTML généré par legacy.js, mais ne sont publiées sur "
        "`window` par aucun module ES — les clics échoueront silencieusement "
        "(ReferenceError) :\n"
        + "\n".join("  - " + n for n in sorted(missing))
    )


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
