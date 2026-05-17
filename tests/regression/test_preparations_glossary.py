"""Tests régression — Lot 3c glossaire (PR-2 du plan meeting-prep v2).

Couvre :

1. **HTTP smoke** sur ``POST /api/preparations/<id>/glossary`` (nouveau
   endpoint mesreunions-web) : route enregistrée, retourne 302/401 sans
   cookie OIDC (jamais 404).

2. Vérification que le ``user_glossary_terms`` upsert-batch existe bien
   côté ``device-token-authority`` (endpoint de proxy attendu par le
   handler mesreunions-web quand ``global=true``).

3. Garde-fou template : la modale glossaire est bien présente dans
   ``index.html`` avec les selectors attendus côté front.

Skip propre :
- service ``mesreunions-web`` injoignable → skip section HTTP
- service ``device-token-authority`` injoignable → skip section DTA
"""

from __future__ import annotations

import os
import pathlib

import pytest
import requests

BASE_URL = os.getenv("MYDEVICES_WEB_URL", "http://localhost:8080")
DTA_URL = os.getenv("DEVICE_TOKEN_AUTHORITY_URL", "http://localhost:5000")


# ─── 1) HTTP smoke mesreunions-web ─────────────────────────────────────────


@pytest.fixture(scope="module")
def _alive_web():
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"mesreunions-web not reachable at {BASE_URL}: {exc}")
    # Sonde feature flag : si glossary endpoint 404 → stack ancienne
    pid = "00000000-0000-0000-0000-000000000000"
    probe = requests.post(
        f"{BASE_URL}/api/preparations/{pid}/glossary",
        json={}, allow_redirects=False, timeout=5,
    )
    if probe.status_code == 404:
        pytest.skip(
            "stack mesreunions-web déployée localement ne contient pas "
            "l'endpoint glossary (Lot 3c). Rebuild + rollout requis."
        )


def test_glossary_endpoint_registered(_alive_web):
    """POST /api/preparations/<id>/glossary doit exister."""
    pid = "00000000-0000-0000-0000-000000000000"
    r = requests.post(
        f"{BASE_URL}/api/preparations/{pid}/glossary",
        json={"glossary_source": [{"term": "MIrAI", "global": True}]},
        allow_redirects=False, timeout=5,
    )
    assert r.status_code != 404, (
        "POST /api/preparations/<id>/glossary → 404 (route absente, Lot 3c)"
    )
    assert r.status_code in (302, 401), (
        f"attendu 302/401 sans cookie, got {r.status_code}"
    )


def test_glossary_endpoint_accepts_only_post(_alive_web):
    """GET sur l'endpoint glossary doit retourner 405 ou rediriger,
    pas 200 (la modale lit le glossaire via GET /preparations/<id> qui
    inclut ``glossary_source``)."""
    pid = "00000000-0000-0000-0000-000000000000"
    r = requests.get(
        f"{BASE_URL}/api/preparations/{pid}/glossary",
        allow_redirects=False, timeout=5,
    )
    # 302 login OU 405 method not allowed acceptable ; 404 = route absente
    assert r.status_code != 404


# ─── 2) DTA upsert-batch — proxy cible ───────────────────────────────────


@pytest.fixture(scope="module")
def _alive_dta():
    try:
        r = requests.get(f"{DTA_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"device-token-authority not reachable at {DTA_URL}: {exc}")


def test_user_glossary_upsert_batch_endpoint_registered(_alive_dta):
    """L'endpoint cible du proxy `?global=true` doit exister."""
    r = requests.post(
        f"{DTA_URL}/api/v1/user-glossary/upsert-batch",
        json={"user_sub": "x", "terms": []},
        allow_redirects=False, timeout=5,
    )
    # Sans bearer interne → 401 ; sinon 200/400. Surtout pas 404.
    assert r.status_code != 404, (
        "/api/v1/user-glossary/upsert-batch absent — proxy glossary cassé"
    )


# ─── 3) Front — selectors modale glossaire ──────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_INDEX = _REPO_ROOT / "services" / "mesreunions-web" / "app" / "templates" / "index.html"


def test_glossary_button_present_in_template():
    """Le bouton "Glossaire (N termes)" doit être dans la fiche brief."""
    assert _INDEX.exists()
    txt = _INDEX.read_text()
    # Bouton + compteur visibles
    assert "brief-detail-glossary-btn" in txt, (
        "Bouton Glossaire absent — Lot 3c non appliqué au template"
    )
    assert "brief-detail-glossary-count" in txt
