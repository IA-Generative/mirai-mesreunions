"""Tests de l'endpoint de navigation Drive du wizard.

Le point de conception que ces tests figent : contrairement à
``test-drive`` (diagnostic, qui répond 200 avec un champ ``error``), la
navigation rend de vrais codes HTTP — le front doit pouvoir distinguer
« pas accès » de « introuvable » de « Drive en panne ».
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("flask")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_meeting_prep_route import _load_mesreunions_web, _login  # noqa: E402


@pytest.fixture
def web(monkeypatch):
    mod = _load_mesreunions_web()
    import libs.shared.app.config as cfg
    for key, value in {
        "DRIVE_BASE_URL": "https://drive.test",
        "OIDC_TOKEN_ENDPOINT": "https://kc.test/token",
        "DRIVE_HOST_ROUTES": {"beta.test": "https://beta.test",
                              "dinum.test": "https://dinum.test"},
    }.items():
        monkeypatch.setattr(cfg, key, value, raising=False)
        monkeypatch.setattr(mod, key, value, raising=False)
    mod.app.config["TESTING"] = True
    return mod.app.test_client(), mod


def _drive_returning(items):
    inst = MagicMock()
    inst.exchange_refresh.return_value = "AT"
    inst.list_children_paginated.return_value = items
    inst.list_roots.return_value = items
    return MagicMock(return_value=inst), inst


def test_instances_are_derived_from_host_routes(web):
    client, _mod = web
    _login(client)
    r = client.get("/api/preparations/drive/instances")
    assert r.status_code == 200
    keys = [d["key"] for d in r.get_json()["drives"]]
    assert "beta" in keys and "dinum" in keys


def test_browse_splits_folders_and_files(web):
    client, mod = web
    _login(client)
    cls, _inst = _drive_returning([
        {"id": "d1", "title": "Archives", "type": "folder"},
        {"id": "f1", "title": "Note.pdf", "mime_type": "application/pdf", "size": 1234},
    ])
    with patch.object(mod._meeting_prep, "DriveClient", cls), \
         patch("libs.shared.app.oidc_refresh_store.fetch_ciphertext", return_value=b"x"), \
         patch("libs.shared.app.secrets_crypto.decrypt", return_value="RT"):
        r = client.get("/api/preparations/drive/browse?folder_id=abc")

    assert r.status_code == 200
    body = r.get_json()
    assert [f["name"] for f in body["folders"]] == ["Archives"]
    assert [f["name"] for f in body["files"]] == ["Note.pdf"]
    assert body["files"][0]["mime_type"] == "application/pdf"
    assert body["is_root"] is False


def test_browse_without_folder_lists_roots(web):
    client, mod = web
    _login(client)
    cls, inst = _drive_returning([{"id": "w1", "title": "Mon espace", "type": "folder"}])
    with patch.object(mod._meeting_prep, "DriveClient", cls), \
         patch("libs.shared.app.oidc_refresh_store.fetch_ciphertext", return_value=b"x"), \
         patch("libs.shared.app.secrets_crypto.decrypt", return_value="RT"):
        r = client.get("/api/preparations/drive/browse")

    assert r.status_code == 200
    assert r.get_json()["is_root"] is True
    inst.list_roots.assert_called_once()
    inst.list_children_paginated.assert_not_called()


def test_browse_uses_the_paginated_listing(web):
    """Un dossier de plus de 20 entrées doit être rendu en entier."""
    client, mod = web
    _login(client)
    many = [{"id": f"f{i}", "title": f"Doc {i}.txt"} for i in range(45)]
    cls, inst = _drive_returning(many)
    with patch.object(mod._meeting_prep, "DriveClient", cls), \
         patch("libs.shared.app.oidc_refresh_store.fetch_ciphertext", return_value=b"x"), \
         patch("libs.shared.app.secrets_crypto.decrypt", return_value="RT"):
        r = client.get("/api/preparations/drive/browse?folder_id=abc")

    assert len(r.get_json()["files"]) == 45
    inst.list_children_paginated.assert_called_once()


@pytest.mark.parametrize("exc_name,status", [
    ("DriveApplicativeError", 404),
    ("DriveTransientError", 502),
])
def test_browse_maps_drive_errors_to_real_status_codes(web, exc_name, status):
    client, mod = web
    _login(client)
    inst = MagicMock()
    inst.exchange_refresh.return_value = "AT"
    inst.list_children_paginated.side_effect = getattr(mod._meeting_prep, exc_name)("boom")
    with patch.object(mod._meeting_prep, "DriveClient", MagicMock(return_value=inst)), \
         patch("libs.shared.app.oidc_refresh_store.fetch_ciphertext", return_value=b"x"), \
         patch("libs.shared.app.secrets_crypto.decrypt", return_value="RT"):
        r = client.get("/api/preparations/drive/browse?folder_id=abc")
    assert r.status_code == status


def test_browse_forbidden_folder_is_403_not_200(web):
    client, mod = web
    _login(client)
    inst = MagicMock()
    inst.exchange_refresh.return_value = "AT"
    inst.list_children_paginated.side_effect = mod._meeting_prep.DriveAuthError(
        "nope", status_code=403)
    with patch.object(mod._meeting_prep, "DriveClient", MagicMock(return_value=inst)), \
         patch("libs.shared.app.oidc_refresh_store.fetch_ciphertext", return_value=b"x"), \
         patch("libs.shared.app.secrets_crypto.decrypt", return_value="RT"):
        r = client.get("/api/preparations/drive/browse?folder_id=abc")
    assert r.status_code == 403
    assert r.get_json()["code"] == "drive_forbidden"


def test_browse_without_stored_token_is_401(web):
    client, mod = web
    _login(client)
    with patch("libs.shared.app.oidc_refresh_store.fetch_ciphertext", return_value=None):
        r = client.get("/api/preparations/drive/browse")
    assert r.status_code == 401
    assert r.get_json()["code"] == "no_refresh_token"


def test_browse_rejects_invalid_folder_reference(web):
    client, _mod = web
    _login(client)
    r = client.get("/api/preparations/drive/browse?folder_id=" + "https://x/nope/")
    assert r.status_code == 400
