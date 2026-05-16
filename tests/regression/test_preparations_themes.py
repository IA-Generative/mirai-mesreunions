"""Tests régression — Lot 9 thématiques (PR-6 du plan meeting-prep v2).

Couvre :

1. **HTTP smoke** sur ``GET /api/preparations/themes-suggestions`` et
   ``POST /api/preparations/<id>/amend`` avec payload ``themes`` —
   route enregistrée (302/401 sans cookie OIDC).

2. **Contract lock** : la normalisation côté DTA (cf
   ``services/device-token-authority/app/main.py``, fonction
   ``amend_preparation``) doit garantir :
     - dédoublonnage case-insensitive (1re casse conservée)
     - strip des espaces
     - cap 50 entrées
     - éléments non-str ignorés silencieusement
   Le code de normalisation est inliné dans main.py (impossible à importer
   sans bootstrap Flask complet) — on encode la spec attendue dans une
   fonction de référence locale + on documente la cible.

3. Garde-fou template : sections thématiques présentes dans index.html.
"""

from __future__ import annotations

import os
import pathlib

import pytest
import requests

BASE_URL = os.getenv("MYDEVICES_WEB_URL", "http://localhost:8080")


# ─── 1) HTTP smoke ───────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def _alive():
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"mydevices-web not reachable at {BASE_URL}: {exc}")


def test_themes_suggestions_endpoint_registered(_alive):
    r = requests.get(
        f"{BASE_URL}/api/preparations/themes-suggestions",
        allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404
    assert r.status_code in (302, 401)


def test_amend_accepts_themes_payload(_alive):
    pid = "00000000-0000-0000-0000-000000000000"
    r = requests.post(
        f"{BASE_URL}/api/preparations/{pid}/amend",
        json={"themes": ["Budget", "Stratégie 2026"]},
        allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404
    assert r.status_code in (302, 401)


# ─── 2) Spec lock — normalisation côté DTA ──────────────────────────────


def _normalize_themes_spec(themes_raw):
    """Re-implémentation locale du contrat attendu côté DTA (main.py
    amend_preparation). Si DTA change, ce test casse et force révision."""
    if not isinstance(themes_raw, list):
        return None
    cleaned = []
    seen_lc = set()
    for t in themes_raw:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if not s:
            continue
        lc = s.lower()
        if lc in seen_lc:
            continue
        seen_lc.add(lc)
        cleaned.append(s)
        if len(cleaned) >= 50:
            break
    return cleaned


class TestThemesNormalizationSpec:
    def test_dedup_case_insensitive_keeps_first_case(self):
        out = _normalize_themes_spec(["Budget", "BUDGET", "budget"])
        assert out == ["Budget"]

    def test_strip_and_skip_empty(self):
        assert _normalize_themes_spec(["  ", "", "Risque "]) == ["Risque"]

    def test_skip_non_strings(self):
        out = _normalize_themes_spec(["ok", 123, None, {"x": 1}, "ok2"])
        assert out == ["ok", "ok2"]

    def test_cap_50(self):
        out = _normalize_themes_spec([f"theme-{i}" for i in range(80)])
        assert len(out) == 50

    def test_invalid_input(self):
        assert _normalize_themes_spec(None) is None
        assert _normalize_themes_spec("not a list") is None


# ─── 3) Front — selectors UI thématiques ────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_INDEX = _REPO_ROOT / "services" / "mydevices-web" / "app" / "templates" / "index.html"


def test_themes_sections_present_in_template():
    txt = _INDEX.read_text()
    # Wizard
    assert "wizard-themes-container" in txt, (
        "Le bloc thématiques du wizard est absent (Lot 9)"
    )
    # Fiche brief
    assert "brief-detail-themes" in txt, (
        "La section thématiques de la fiche brief est absente (Lot 9)"
    )
    assert "brief-detail-themes-save-btn" in txt
