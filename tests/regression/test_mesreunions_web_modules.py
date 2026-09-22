"""End-to-end tests pour les blueprints PR3 de ``mesreunions-web``.

Vérifie que :
- le service répond sur ``/healthz``,
- les nouveaux endpoints ``/api/preparations/*`` et ``/api/meetings/*``
  sont enregistrés (302 redirect vers login sans cookie OIDC, pas 404),
- les anciens endpoints ``/api/meeting-prep/*`` ont bien été retirés
  (404),
- aucun alias ``meeting_brief_id`` ne survit dans le template Jinja.

Exécution contre la stack docker compose locale (overlay shared-infra) :

    cd deploy/docker
    docker compose -f docker-compose.yml -f docker-compose.shared-infra.yml up -d

mesreunions-web est exposé sur ``http://localhost:8080``. Les tests sont
volontairement légers — ils n'utilisent pas de session OIDC valide ; on
vérifie le routing et la présence des handlers.
"""

from __future__ import annotations

import os
import pathlib

import pytest
import requests

BASE_URL = os.getenv("MYDEVICES_WEB_URL", "http://localhost:8080")


@pytest.fixture(scope="module")
def alive():
    try:
        r = requests.get(f"{BASE_URL}/healthz", timeout=2)
        r.raise_for_status()
    except Exception as exc:
        pytest.skip(f"mesreunions-web not reachable at {BASE_URL}: {exc}")


# ─── Healthz ─────────────────────────────────────────────────


def test_healthz_200(alive):
    r = requests.get(f"{BASE_URL}/healthz", timeout=5)
    assert r.status_code == 200


# ─── Blueprint preparations : endpoints enregistrés ─────────

PREPARATION_ENDPOINTS = [
    # (method, path) — chaque endpoint doit redirect 302 (login) ou 401.
    ("GET",    "/api/preparations"),
    ("GET",    "/api/preparations/"),
    ("GET",    "/api/preparations/test-drive"),
    ("GET",    "/api/preparations/drive/browse"),
    ("GET",    "/api/preparations/drive/instances"),
    ("GET",    "/api/preparations/link-suggestion"),
    ("GET",    "/api/preparations/00000000-0000-0000-0000-000000000000"),
    ("GET",    "/api/preparations/00000000-0000-0000-0000-000000000000/audio-files"),
    ("GET",    "/api/preparations/00000000-0000-0000-0000-000000000000/series"),
    ("POST",   "/api/preparations"),
    ("POST",   "/api/preparations/00000000-0000-0000-0000-000000000000/amend"),
    ("POST",   "/api/preparations/00000000-0000-0000-0000-000000000000/rename"),
    ("POST",   "/api/preparations/00000000-0000-0000-0000-000000000000/restore"),
    ("POST",   "/api/preparations/00000000-0000-0000-0000-000000000000/link-audio"),
    ("POST",   "/api/preparations/unlink-audio"),
    ("PUT",    "/api/preparations/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/api/preparations/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/api/preparations/00000000-0000-0000-0000-000000000000/permanently"),
]


@pytest.mark.parametrize("method,path", PREPARATION_ENDPOINTS)
def test_preparations_endpoint_auth_required(alive, method, path):
    """Sans cookie OIDC → 302 redirect vers /login (jamais 404)."""
    r = requests.request(
        method, f"{BASE_URL}{path}",
        allow_redirects=False,
        timeout=5,
    )
    # Authentifié = 200/204 ; non authentifié = 302 vers /login ; jamais 404.
    assert r.status_code != 404, (
        f"{method} {path} → 404 : endpoint non enregistré"
    )
    assert r.status_code in (302, 401), (
        f"{method} {path} → {r.status_code} (attendu 302/401)"
    )


# ─── Blueprint meetings : endpoints enregistrés ─────────────

MEETING_ENDPOINTS = [
    ("GET",    "/api/meetings"),
    ("GET",    "/api/meetings/"),
    ("GET",    "/api/meetings/00000000-0000-0000-0000-000000000000"),
    ("POST",   "/api/meetings"),
    ("POST",   "/api/meetings/00000000-0000-0000-0000-000000000000/amend"),
    ("POST",   "/api/meetings/00000000-0000-0000-0000-000000000000/rename"),
    ("POST",   "/api/meetings/00000000-0000-0000-0000-000000000000/restore"),
    ("POST",   "/api/meetings/00000000-0000-0000-0000-000000000000/link-preparation"),
    ("POST",   "/api/meetings/00000000-0000-0000-0000-000000000000/link-audio"),
    ("POST",   "/api/meetings/00000000-0000-0000-0000-000000000000/reprocess"),
    ("PUT",    "/api/meetings/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/api/meetings/00000000-0000-0000-0000-000000000000"),
    ("DELETE", "/api/meetings/00000000-0000-0000-0000-000000000000/permanently"),
]


@pytest.mark.parametrize("method,path", MEETING_ENDPOINTS)
def test_meetings_endpoint_auth_required(alive, method, path):
    r = requests.request(
        method, f"{BASE_URL}{path}",
        allow_redirects=False,
        timeout=5,
    )
    assert r.status_code != 404, (
        f"{method} {path} → 404 : endpoint non enregistré"
    )
    assert r.status_code in (302, 401), (
        f"{method} {path} → {r.status_code} (attendu 302/401)"
    )


# ─── Legacy /api/meeting-prep/* : doivent être retirés ──────

LEGACY_ENDPOINTS = [
    ("GET",    "/api/meeting-prep"),
    ("POST",   "/api/meeting-prep"),
    ("GET",    "/api/meeting-prep/test-drive"),
    ("GET",    "/api/meeting-prep/link-suggestion"),
    ("GET",    "/api/meeting-prep/00000000-0000-0000-0000-000000000000"),
    ("POST",   "/api/meeting-prep/00000000-0000-0000-0000-000000000000/rename"),
    ("POST",   "/api/meeting-prep/00000000-0000-0000-0000-000000000000/amend"),
    ("DELETE", "/api/meeting-prep/00000000-0000-0000-0000-000000000000"),
    ("POST",   "/api/meeting-prep/00000000-0000-0000-0000-000000000000/restore"),
    ("DELETE", "/api/meeting-prep/00000000-0000-0000-0000-000000000000/permanently"),
    ("GET",    "/api/meeting-prep/00000000-0000-0000-0000-000000000000/audio-files"),
    ("GET",    "/api/meeting-prep/00000000-0000-0000-0000-000000000000/series"),
    ("POST",   "/api/file/00000000-0000-0000-0000-000000000000/link-brief"),
    ("POST",   "/api/file/00000000-0000-0000-0000-000000000000/reprocess"),
]


@pytest.mark.parametrize("method,path", LEGACY_ENDPOINTS)
def test_legacy_meeting_prep_endpoints_removed(alive, method, path):
    """Doivent être 404 — supprimés en PR3."""
    r = requests.request(
        method, f"{BASE_URL}{path}",
        allow_redirects=False,
        timeout=5,
    )
    assert r.status_code == 404, (
        f"{method} {path} → {r.status_code} (legacy attendu 404 / supprimé en PR3)"
    )


# ─── Front : aucun ``meeting_brief_id`` ni ``/api/meeting-prep`` ───

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "services" / "mesreunions-web" / "app" / "templates" / "index.html"


def test_front_index_no_legacy_api_paths():
    """templates/index.html ne contient plus ``/api/meeting-prep`` ni
    ``meeting_brief_id`` (sauf en commentaire). Les références aux pages
    ``/meeting-prep`` et ``/meeting-prep/new`` restent autorisées (routes
    serveur HTML conservées)."""
    assert INDEX_HTML.exists(), f"introuvable : {INDEX_HTML}"
    text = INDEX_HTML.read_text()
    # API paths : aucun /api/meeting-prep restant
    assert "/api/meeting-prep" not in text, (
        "Le front contient encore une référence à /api/meeting-prep — "
        "doit être remplacé par /api/preparations ou /api/meetings"
    )
    # Champ legacy meeting_brief_id : doit avoir disparu côté payloads.
    assert "meeting_brief_id" not in text, (
        "Le front contient encore meeting_brief_id — doit être "
        "preparation_id ou meeting_id selon le contexte"
    )
