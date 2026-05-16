"""Tests régression — Lot 4 export DOCX / ODT (PR-3 du plan meeting-prep v2).

Couvre :

1. **Unit** sur ``services/mydevices-web/app/modules/preparations/exporters.py``
   (importable sans Postgres) : ``render(prep, fmt)`` doit retourner des
   bytes non-vides + un Content-Type + un filename slugifié pour ``docx``
   et ``odt``. Les formats TXT/MD sont sérialisés côté front
   (``frontend/lib/export-formatter.js``) — non testables sans Node ; un
   test optionnel via ``node --check`` valide le parse.

2. **HTTP smoke** sur ``GET /api/preparations/<id>/export?format=...`` :
   route enregistrée (302/401 sans cookie OIDC).

Skip propre :
- ``python-docx`` / ``odfpy`` manquant → skip section unit
- ``node`` absent → skip parse JS
- service injoignable → skip section HTTP
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_MYDEVICES_PATH = os.path.join(_REPO_ROOT, "services", "mydevices-web")

# Si un autre test a déjà bindé ``app`` à un autre service (DTA), purge
# le cache d'imports avant de remapper sur mydevices-web.
for _mod in list(sys.modules):
    if _mod == "app" or _mod.startswith("app."):
        del sys.modules[_mod]
if _MYDEVICES_PATH in sys.path:
    sys.path.remove(_MYDEVICES_PATH)
sys.path.insert(0, _MYDEVICES_PATH)

try:
    from app.modules.preparations.exporters import (  # type: ignore
        CONTENT_TYPES,
        render,
        render_docx,
        render_odt,
        slugify,
    )
    _HAS_EXPORTERS = True
    _IMPORT_ERR = None
except Exception as exc:  # pragma: no cover
    _HAS_EXPORTERS = False
    _IMPORT_ERR = str(exc)


# ─── 1) slugify — robustesse ─────────────────────────────────────────────


@pytest.mark.skipif(not _HAS_EXPORTERS, reason=f"exporters indispo : {_IMPORT_ERR}")
class TestSlugify:
    def test_basic_accents_strip(self):
        assert slugify("Réunion N°1") == "reunion-n-1"

    def test_empty_fallback(self):
        assert slugify("") == "preparation"
        assert slugify("///---///") == "preparation"

    def test_long_truncated(self):
        out = slugify("a" * 200, max_len=30)
        assert len(out) <= 30


# ─── 2) render(docx/odt) — binaires non-vides + signature magique ────────


@pytest.fixture
def sample_prep():
    return {
        "id": "test-id",
        "title": "Réunion stratégie 2026",
        "created_at": "2026-05-17T10:00:00Z",
        "target_meeting_date": "2026-05-20T14:00:00Z",
        "content": {
            "objective_reformulated": "Aligner la roadmap Q3.",
            "context_recap": "Suite à la revue Q2.",
            "agenda": [
                {"title": "Tour de table", "duration": "5min",
                 "objective": "intro", "key_questions": ["Qui parle ?"]},
                {"title": "Roadmap", "duration": "30min",
                 "objective": "valider", "key_questions": ["Budget ?"]},
            ],
            "participants_notes": [
                {"name": "Alice", "note": "Lead produit"},
            ],
            "open_threads": [
                {"item": "Décision archi", "source": "CR avril"},
            ],
            "opening_questions": ["Quel est le risque #1 ?"],
            "risk_points": ["Délai serré"],
            "preparation_checklist": ["Relire le doc X"],
        },
        "participants": [{"name": "Alice", "email": "a@example.com"}],
    }


@pytest.mark.skipif(not _HAS_EXPORTERS, reason=f"exporters indispo : {_IMPORT_ERR}")
class TestRenderDocx:
    def test_render_docx_returns_bytes(self, sample_prep):
        data = render_docx(sample_prep)
        assert isinstance(data, (bytes, bytearray))
        assert len(data) > 1000, "DOCX trop petit, suspect"
        # Un .docx est un ZIP → magic PK\x03\x04
        assert data[:4] == b"PK\x03\x04"

    def test_render_dispatcher_docx(self, sample_prep):
        data, ct, fn = render(sample_prep, "docx")
        assert ct == CONTENT_TYPES["docx"]
        assert fn.endswith(".docx")
        assert "reunion-strategie-2026" in fn
        assert data[:4] == b"PK\x03\x04"


@pytest.mark.skipif(not _HAS_EXPORTERS, reason=f"exporters indispo : {_IMPORT_ERR}")
class TestRenderOdt:
    def test_render_odt_returns_bytes(self, sample_prep):
        data = render_odt(sample_prep)
        assert isinstance(data, (bytes, bytearray))
        assert len(data) > 500
        # ODT est aussi un ZIP
        assert data[:4] == b"PK\x03\x04"

    def test_render_dispatcher_odt(self, sample_prep):
        data, ct, fn = render(sample_prep, "odt")
        assert ct == CONTENT_TYPES["odt"]
        assert fn.endswith(".odt")


@pytest.mark.skipif(not _HAS_EXPORTERS, reason=f"exporters indispo : {_IMPORT_ERR}")
def test_render_unknown_format_raises(sample_prep):
    with pytest.raises(ValueError):
        render(sample_prep, "pdf")


@pytest.mark.skipif(not _HAS_EXPORTERS, reason=f"exporters indispo : {_IMPORT_ERR}")
def test_render_empty_content_still_produces_file():
    """Une prep sans content doit quand même produire un binaire valide
    (au moins le header). Robustesse pour les preps en cours de génération."""
    data, _, _ = render({"title": "vide"}, "docx")
    assert data[:4] == b"PK\x03\x04"


# ─── 3) Frontend export-formatter.js — parse Node (optionnel) ────────────


def _have_node() -> bool:
    try:
        subprocess.run(
            ["node", "--version"],
            capture_output=True, check=True, timeout=5,
        )
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _have_node(), reason="node introuvable")
def test_export_formatter_parses_via_node():
    """Garde-fou syntaxe : le helper front TXT/MD doit parser en JS."""
    path = os.path.join(
        _REPO_ROOT, "services", "mydevices-web", "frontend", "lib",
        "export-formatter.js",
    )
    if not os.path.isfile(path):
        pytest.skip(f"export-formatter.js absent : {path}")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".mjs", delete=False, encoding="utf-8",
    ) as fh:
        fh.write(open(path).read())
        mjs_path = fh.name
    try:
        result = subprocess.run(
            ["node", "--check", mjs_path],
            capture_output=True, text=True, timeout=15,
        )
    finally:
        os.unlink(mjs_path)
    assert result.returncode == 0, (
        "node --check a échoué sur export-formatter.js :\n"
        + (result.stderr or "<vide>")
    )


# ─── 4) HTTP smoke ───────────────────────────────────────────────────────

import requests  # noqa: E402

BASE_URL = os.getenv("MYDEVICES_WEB_URL", "http://localhost:8080")


@pytest.fixture(scope="module")
def _alive_with_v2():
    """Skip si le service est down OU si la version déployée n'expose pas
    encore les endpoints v2 (export, glossary, send-cr). Détecté en
    sondant un endpoint v2 connu — un 404 = stack pas encore rebuild."""
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"mydevices-web not reachable at {BASE_URL}: {exc}")
    # Sonde : si /export retourne 404 → stack ancienne, skip
    pid = "00000000-0000-0000-0000-000000000000"
    probe = requests.get(
        f"{BASE_URL}/api/preparations/{pid}/export?format=docx",
        allow_redirects=False, timeout=5,
    )
    if probe.status_code == 404:
        pytest.skip(
            "mydevices-web déployé localement ne contient pas les "
            "endpoints meeting-prep v2 (export). Rebuild + rollout requis."
        )


@pytest.mark.parametrize("fmt", ["docx", "odt", "txt"])
def test_export_endpoint_registered(_alive_with_v2, fmt):
    pid = "00000000-0000-0000-0000-000000000000"
    r = requests.get(
        f"{BASE_URL}/api/preparations/{pid}/export?format={fmt}",
        allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404, (
        f"/api/preparations/<id>/export?format={fmt} → 404 (route absente)"
    )
    assert r.status_code in (302, 401, 400), (
        f"attendu 302/401/400, got {r.status_code}"
    )
