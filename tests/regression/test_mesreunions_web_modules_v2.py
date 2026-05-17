"""End-to-end tests pour les blueprints PR3-v2 de ``mesreunions-web``.

Verifie que les nouveaux modules ``auth`` / ``devices`` / ``sessions``
sont bien enregistres et que leurs endpoints repondent correctement :

- sans cookie OIDC -> 302 redirect vers /login (jamais 404)
- les routes proxy (enroll-proxy, validate-proxy) -> 401 sans bearer

Le test n'instancie pas l'app Python — il tape la stack docker compose locale
(http://localhost:8080) si disponible, sinon skip.
"""

from __future__ import annotations

import os

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


# ─── Module auth ─────────────────────────────────────────────────────────


AUTH_ENDPOINTS = [
    ("GET", "/login"),
    ("GET", "/auth/callback"),
    ("GET", "/logout"),
]


@pytest.mark.parametrize("method,path", AUTH_ENDPOINTS)
def test_auth_route_registered(alive, method, path):
    """Les routes OIDC doivent exister (pas 404) — meme sans creds."""
    r = requests.request(method, f"{BASE_URL}{path}",
                         allow_redirects=False, timeout=5)
    assert r.status_code != 404, f"{method} {path} -> 404 (route absente)"


def test_login_redirects_to_keycloak(alive):
    """/login doit rediriger vers le provider OIDC (302/303)."""
    r = requests.get(f"{BASE_URL}/login", allow_redirects=False, timeout=5)
    assert r.status_code in (302, 303), (
        f"/login devrait rediriger vers Keycloak, got {r.status_code}"
    )
    # Le Location pointe vers /protocol/openid-connect/auth
    loc = r.headers.get("Location", "")
    assert "openid-connect/auth" in loc or "/auth" in loc, (
        f"Location inattendue : {loc!r}"
    )


# ─── Module devices ──────────────────────────────────────────────────────


DEVICE_AUTH_ENDPOINTS = [
    ("POST",   "/api/generate-code"),
    ("GET",    "/api/qr-image/abc123"),
    ("GET",    "/api/my-devices"),
    ("POST",   "/api/my-devices/abc/rename"),
    ("POST",   "/api/my-devices/abc/revoke"),
    ("DELETE", "/api/my-devices/abc"),
    ("POST",   "/api/my-devices/revoke-all"),
    ("POST",   "/api/my-token/renew-7d"),
    ("POST",   "/api/my-sessions/abc/renew-7d"),
]


@pytest.mark.parametrize("method,path", DEVICE_AUTH_ENDPOINTS)
def test_devices_endpoint_auth_required(alive, method, path):
    """Sans cookie OIDC -> 302 vers /login (jamais 404)."""
    r = requests.request(method, f"{BASE_URL}{path}",
                         allow_redirects=False, timeout=5)
    assert r.status_code in (302, 303, 401), (
        f"{method} {path} -> {r.status_code} (attendu 302/303/401)"
    )
    assert r.status_code != 404


DEVICE_PROXY_ENDPOINTS = [
    "/api/device/enroll-proxy",
    "/api/device/validate-proxy",
]


@pytest.mark.parametrize("path", DEVICE_PROXY_ENDPOINTS)
def test_devices_proxy_requires_bearer(alive, path):
    """Endpoints proxy : 401 sans bearer (auth INTERNAL_API_TOKEN)."""
    r = requests.post(f"{BASE_URL}{path}", json={},
                      allow_redirects=False, timeout=5)
    assert r.status_code == 401, (
        f"POST {path} sans bearer -> {r.status_code} (attendu 401)"
    )


# ─── Module sessions ─────────────────────────────────────────────────────


SESSION_AUTH_ENDPOINTS = [
    ("GET",    "/api/my-sessions"),
    ("GET",    "/api/my-trash"),
    ("POST",   "/api/my-upload"),
    ("POST",   "/api/purge-my-sessions"),
    ("DELETE", "/api/my-sessions/abc"),
    ("POST",   "/api/my-sessions/abc/restore"),
    ("DELETE", "/api/file/abc"),
    ("DELETE", "/api/file/abc/permanently"),
    ("POST",   "/api/file/abc/restore"),
    ("POST",   "/api/file/abc/rename"),
    ("PATCH",  "/api/file/abc/meeting-datetime"),
    ("GET",    "/api/file/transcript-status/abc"),
    ("GET",    "/api/file/normalization-impact/abc"),
    ("GET",    "/api/file/download/abc"),
    ("GET",    "/api/file/stream/abc"),
    ("GET",    "/api/file/download-source/abc"),
    ("GET",    "/api/file/stream-source/abc"),
    ("GET",    "/api/file/download-transcoded/abc"),
    ("GET",    "/api/file/stream-transcoded/abc"),
    ("GET",    "/api/file/download-transferred/abc"),
    ("GET",    "/api/file/stream-transferred/abc"),
    ("GET",    "/api/file/transcript/transcript/txt/abc"),
    ("GET",    "/api/file/meeting-cr/md/abc"),
]


@pytest.mark.parametrize("method,path", SESSION_AUTH_ENDPOINTS)
def test_sessions_endpoint_auth_required(alive, method, path):
    r = requests.request(method, f"{BASE_URL}{path}",
                         allow_redirects=False, timeout=5)
    assert r.status_code in (302, 303, 401), (
        f"{method} {path} -> {r.status_code} (attendu 302/303/401)"
    )
    assert r.status_code != 404, f"{method} {path} -> 404 (route absente)"


def test_queue_status_dual_auth(alive):
    """/api/queue-status : sans auth -> 401."""
    r = requests.get(f"{BASE_URL}/api/queue-status",
                     allow_redirects=False, timeout=5)
    assert r.status_code == 401


# ─── Garde-fou : main.py reste minimal ───────────────────────────────────


def test_main_py_reduced_to_bootstrap():
    """PR3-v2 : main.py doit rester <= 500 lignes (bootstrap shell)."""
    import pathlib
    main_py = pathlib.Path(__file__).resolve().parents[2] / (
        "services/mesreunions-web/app/main.py"
    )
    if not main_py.exists():
        pytest.skip("main.py introuvable")
    lines = main_py.read_text(encoding="utf-8").count("\n")
    assert lines <= 500, (
        f"main.py = {lines} lignes ; cible PR3-v2 <= 500 (shell minimal)"
    )


def test_modules_blueprints_files_exist():
    """Verifie la structure de modules attendue post-PR3-v2."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    base = root / "services/mesreunions-web/app/modules"
    for module in ("auth", "devices", "sessions", "drive_sync",
                   "preparations", "meetings", "glossary"):
        assert (base / module / "__init__.py").exists(), (
            f"module {module} : __init__.py manquant"
        )
    # Modules avec routes Flask (blueprints) doivent avoir routes.py.
    for module in ("auth", "devices", "sessions", "preparations", "meetings"):
        assert (base / module / "routes.py").exists(), (
            f"module {module} : routes.py manquant"
        )
