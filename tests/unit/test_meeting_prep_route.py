"""
Unit tests for the Flask routes of the meeting-prep wizard, hosted in
``services/code-generator/app/main.py`` :

  - POST /api/meeting-prep                : génération d'un brief
  - GET  /api/meeting-prep/test-drive     : diagnostic 3-étapes du bouton
                                            « Tester l'accès »

Le module ``main.py`` a deux particularités gênantes en test :

  * il appelle ``create_app()`` à l'import (init DB + bucket S3) — on stubbe
    ``init_tables`` / ``create_session_factory`` / ``require_strong_shared_secret``
    avant le ``exec_module``.
  * il vit dans un package au tiret (``code-generator``), donc on le charge
    via ``importlib.util.spec_from_file_location`` à la manière de
    test_meeting_prep_persistence.py.

Pour les tests on patch les *symboles importés dans main.py* (et pas les
modules sources), c'est le pattern recommandé par les docs Python pour
``unittest.mock`` : on cible la **liaison locale**, pas la définition.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("flask")


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


_INTERNAL_TOKEN = "***REMOVED-FIXTURE-TOKEN***"  # 35 chars, no banned prefix


def _purge_libs_shared_stubs():
    """Drop stubs left by sibling tests so ``from libs.shared.app.config import …``
    sees the real module rather than a bare ModuleType placeholder."""
    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name in {"libs.shared", "libs"}:
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)


def _load_code_generator():
    """Load services/code-generator/app/main.py with DB/S3 init stubbed.

    Returns the loaded module. The caller can ``monkeypatch.setattr`` on the
    module-level constants (DRIVE_BASE_URL, OIDC_TOKEN_ENDPOINT, …) before
    exercising routes.
    """
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-long-xxxxx")
    # OIDCConfig pulls these from env at construction time (dataclass field
    # defaults). Provide harmless values so authlib.register doesn't choke.
    os.environ.setdefault("OIDC_ISSUER", "https://kc.test/realms/test")
    os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
    os.environ.setdefault("OIDC_CLIENT_SECRET", "test-secret")
    os.environ.setdefault("OIDC_REDIRECT_URI", "http://test/auth/callback")

    _purge_libs_shared_stubs()

    # Plusieurs tests soeurs (test_drive_client, test_llm_client,
    # test_oidc_refresh_store, test_suggest_metadata_step, …) remplacent
    # ``requests`` par un MagicMock léger. authlib + nos handlers ont besoin
    # du vrai module ; on purge le stub s'il en est un (pas de __file__).
    for _name in ("requests", "authlib", "authlib.integrations",
                  "authlib.integrations.flask_client",
                  "authlib.integrations.requests_client"):
        _stub = sys.modules.get(_name)
        if _stub is not None and not getattr(_stub, "__file__", None):
            sys.modules.pop(_name, None)

    # ``main.py`` fait ``from app import meeting_prep`` (le package ``app``
    # est ``services/code-generator/app/``). On rend ce package importable.
    cg_dir = os.path.join(ROOT, "services", "code-generator")
    if cg_dir not in sys.path:
        sys.path.insert(0, cg_dir)
    sys.modules.pop("app", None)
    sys.modules.pop("app.meeting_prep", None)

    # pika non plus n'est pas dans l'env minimal — libs.shared.app.queue_helper
    # l'importe inconditionnellement via le __init__.py du package.
    if "pika" not in sys.modules:
        pika_stub = types.ModuleType("pika")
        pika_stub.BlockingConnection = MagicMock()
        pika_stub.ConnectionParameters = MagicMock()
        pika_stub.PlainCredentials = MagicMock()
        pika_stub.exceptions = types.SimpleNamespace(
            AMQPConnectionError=Exception, ChannelClosedByBroker=Exception,
        )
        sys.modules["pika"] = pika_stub

    # qrcode n'est pas nécessairement installé dans l'env de test : on stubbe
    # une API minimale (QRCode + constants.ERROR_CORRECT_M) — les routes
    # qu'on teste n'exercent jamais make_qr_image.
    if "qrcode" not in sys.modules:
        qr_stub = types.ModuleType("qrcode")
        qr_stub.QRCode = MagicMock()
        consts = types.SimpleNamespace(ERROR_CORRECT_M=0)
        qr_stub.constants = consts
        sys.modules["qrcode"] = qr_stub
        sys.modules["qrcode.constants"] = consts

    # Stub heavy import-time side effects of create_app(): init_tables opens a
    # Postgres connection, create_session_factory the same. require_strong_shared_secret
    # validates INTERNAL_API_TOKEN format — we set a strong one above, but we
    # also want to make sure no extra env validation fails the load.
    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    db_stub.__file__ = "<stub>"  # Survives the _purge_libs_shared_stubs() above.
    sys.modules["libs.shared.app.database"] = db_stub

    # Drop any cached version so each call returns a fresh module.
    sys.modules.pop("code_generator_under_route_test", None)
    spec = importlib.util.spec_from_file_location(
        "code_generator_under_route_test",
        os.path.join(ROOT, "services", "code-generator", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cg(monkeypatch):
    """Test client for code-generator, with module-level config knobs set to
    sane defaults so the meeting-prep routes consider themselves configured.

    Each test can further override these via ``monkeypatch.setattr(mod, …)``.
    """
    mod = _load_code_generator()
    monkeypatch.setattr(mod, "DRIVE_BASE_URL", "https://drive.test", raising=False)
    monkeypatch.setattr(mod, "OIDC_TOKEN_ENDPOINT", "https://kc.test/token", raising=False)
    monkeypatch.setattr(mod, "OIDC_OFFLINE_ACCESS", True, raising=False)
    monkeypatch.setattr(mod, "LITELLM_BASE_URL", "https://litellm.test", raising=False)
    monkeypatch.setattr(mod, "LITELLM_API_KEY", "sk-test", raising=False)
    monkeypatch.setattr(mod, "LLM_MODEL_MEDIUM", "claude-medium", raising=False)
    monkeypatch.setattr(mod, "LLM_HTTP_TIMEOUT_SECONDS", 30, raising=False)
    mod.app.config["TESTING"] = True
    client = mod.app.test_client()
    return client, mod


def _login(client, sub="test-user"):
    """Inject a logged-in user into the Flask session so @require_auth passes."""
    with client.session_transaction() as sess:
        sess["user"] = {"sub": sub, "email": "user@test", "preferred_username": "user"}


# ─── POST /api/meeting-prep ────────────────────────────────────────


def test_post_meeting_prep_without_drive_folder_returns_brief(cg):
    """Sans drive_folder : aucun appel DriveClient ; on reçoit un brief dans le JSON."""
    client, mod = cg
    _login(client)

    fake_llm_instance = MagicMock()
    fake_llm_instance.chat_json.return_value = {
        "executive_summary": "Tour de table budget Q3",
        "risks": ["délai"],
    }
    fake_llm_cls = MagicMock(return_value=fake_llm_instance)
    fake_drive_cls = MagicMock()

    # request_internal_device_api est noyé dans le handler — on le neutralise
    # pour éviter tout appel HTTP vers token-issuer.
    with patch.object(mod._meeting_prep, "LLMClient", fake_llm_cls), \
         patch.object(mod._meeting_prep, "DriveClient", fake_drive_cls), \
         patch.object(mod, "fetch_ciphertext") as fc_mock, \
         patch.object(mod, "request_internal_device_api", return_value={"brief": {"id": "b-1"}}):
        r = client.post("/api/meeting-prep", json={
            "subject": "Décider du budget Q3",
            "role": "anime la réunion",
            "expectation": "obtenir un GO",
            "duration_minutes": 60,
            "focus": ["risques"],
            "meeting_type": "general",
        })

    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert "brief" in body
    assert body["brief"]["executive_summary"] == "Tour de table budget Q3"
    assert body["meeting_type"] == "general"
    # Aucun appel DriveClient — la branche Drive ne doit pas être exécutée.
    fake_drive_cls.assert_not_called()
    # fetch_ciphertext non plus, puisqu'il n'y a pas de folder.
    fc_mock.assert_not_called()


def test_post_meeting_prep_with_drive_folder_but_no_refresh_token_returns_401(cg):
    """drive_folder fourni mais aucun token chiffré stocké → 401 no_refresh_token."""
    client, mod = cg
    _login(client)

    with patch.object(mod._meeting_prep, "LLMClient", MagicMock()), \
         patch.object(mod._meeting_prep, "DriveClient", MagicMock()) as drive_cls, \
         patch.object(mod, "fetch_ciphertext", return_value=None) as fc_mock, \
         patch.object(mod, "request_internal_device_api", return_value={"brief": {}}):
        r = client.post("/api/meeting-prep", json={
            "subject": "Sujet",
            "role": "anime",
            "expectation": "GO",
            "duration_minutes": 30,
            "focus": [],
            "meeting_type": "general",
            "drive_folder": "https://drive.test/items/abc-123",
        })

    assert r.status_code == 401, r.get_data(as_text=True)
    body = r.get_json()
    assert body.get("code") == "no_refresh_token"
    fc_mock.assert_called_once()
    # DriveClient ne doit pas avoir été instancié — on est sorti avant.
    drive_cls.assert_not_called()


def test_post_meeting_prep_one_on_one_loads_correct_prompt(cg):
    """meeting_type=one_on_one → load_prompt_template appelé sur le fichier one_on_one.txt."""
    client, mod = cg
    _login(client)

    seen_paths = []

    def _spy_prompt_path_for_type(meeting_type):
        # On laisse l'implémentation réelle déterminer le path, puis on capture.
        path = mod._meeting_prep.PROMPT_FILES_BY_TYPE.get(meeting_type, "general.txt")
        full = os.path.join(mod._meeting_prep.PROMPTS_DIR, path)
        seen_paths.append((meeting_type, full))
        return full

    fake_llm = MagicMock()
    fake_llm.chat_json.return_value = {"summary": "ok"}

    with patch.object(mod._meeting_prep, "prompt_path_for_type", side_effect=_spy_prompt_path_for_type), \
         patch.object(mod._meeting_prep, "load_prompt_template", return_value="{OBJECTIVE} {DURATION_MINUTES} {ROLE_VIEWPOINT} {EXPECTATION} {FOCUS_AREAS} {PREP_DOCS} {PRIOR_MEETINGS}") as load_tpl, \
         patch.object(mod._meeting_prep, "LLMClient", MagicMock(return_value=fake_llm)), \
         patch.object(mod._meeting_prep, "DriveClient", MagicMock()), \
         patch.object(mod, "fetch_ciphertext", return_value=None), \
         patch.object(mod, "request_internal_device_api", return_value={"brief": {}}):
        r = client.post("/api/meeting-prep", json={
            "subject": "Point hebdo Alice",
            "role": "manager",
            "expectation": "feedback",
            "duration_minutes": 30,
            "focus": [],
            "meeting_type": "one_on_one",
            "drive_folder": "",   # vide → on n'entre pas dans la branche Drive
        })

    assert r.status_code == 200, r.get_data(as_text=True)
    assert seen_paths and seen_paths[0][0] == "one_on_one"
    # load_prompt_template doit recevoir le chemin one_on_one.txt
    arg_path = load_tpl.call_args.args[0]
    assert arg_path.endswith("one_on_one.txt"), f"Mauvais prompt chargé : {arg_path}"


def test_post_meeting_prep_unknown_meeting_type_falls_back_to_general(cg):
    """meeting_type inconnu (« blah ») : fallback silencieux sur ``general``."""
    client, mod = cg
    _login(client)

    fake_llm = MagicMock()
    fake_llm.chat_json.return_value = {"x": 1}

    with patch.object(mod._meeting_prep, "LLMClient", MagicMock(return_value=fake_llm)), \
         patch.object(mod._meeting_prep, "DriveClient", MagicMock()), \
         patch.object(mod, "fetch_ciphertext", return_value=None), \
         patch.object(mod, "request_internal_device_api", return_value={"brief": {}}):
        r = client.post("/api/meeting-prep", json={
            "subject": "Sujet",
            "role": "anime",
            "expectation": "GO",
            "duration_minutes": 30,
            "focus": [],
            "meeting_type": "blah",
            "drive_folder": "",
        })

    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["meeting_type"] == "general"


# ─── GET /api/meeting-prep/test-drive ──────────────────────────────


def test_test_drive_without_refresh_token_returns_diagnostic(cg):
    """Sans token stocké : 200 + token_stored=false + error rempli."""
    client, mod = cg
    _login(client)

    with patch.object(mod, "fetch_ciphertext", return_value=None):
        r = client.get("/api/meeting-prep/test-drive")

    assert r.status_code == 200
    body = r.get_json()
    assert body["token_stored"] is False
    assert body["exchange_ok"] is False
    assert body["drive_reachable"] is False
    assert body["error"]  # message non vide


def test_test_drive_full_success_path(cg):
    """Token stocké + exchange OK + Drive 200 : tous les flags True, error None."""
    client, mod = cg
    _login(client)

    fake_drive = MagicMock()
    fake_drive.exchange_refresh.return_value = "access-token-xyz"

    # Le handler fait `import requests as _req` localement puis _req.get(...).
    # On patch le binding sur le module requests dans sys.modules, vu sous
    # le nom utilisé localement.
    fake_resp = MagicMock()
    fake_resp.status_code = 200

    with patch.object(mod, "fetch_ciphertext", return_value=b"ciphertext"), \
         patch.object(mod, "decrypt_secret", return_value="refresh-xyz"), \
         patch.object(mod._meeting_prep, "DriveClient", MagicMock(return_value=fake_drive)), \
         patch("requests.get", return_value=fake_resp):
        r = client.get("/api/meeting-prep/test-drive")

    assert r.status_code == 200
    body = r.get_json()
    assert body["token_stored"] is True
    assert body["exchange_ok"] is True
    assert body["drive_reachable"] is True
    assert body["error"] is None


def test_test_drive_exchange_rejected_returns_error(cg):
    """Token présent mais Keycloak refuse → exchange_ok=false + error non-nul."""
    client, mod = cg
    _login(client)

    fake_drive = MagicMock()
    fake_drive.exchange_refresh.side_effect = mod._meeting_prep.DriveAuthError("refresh rejected")

    with patch.object(mod, "fetch_ciphertext", return_value=b"ciphertext"), \
         patch.object(mod, "decrypt_secret", return_value="refresh-xyz"), \
         patch.object(mod._meeting_prep, "DriveClient", MagicMock(return_value=fake_drive)):
        r = client.get("/api/meeting-prep/test-drive")

    assert r.status_code == 200
    body = r.get_json()
    assert body["token_stored"] is True
    assert body["exchange_ok"] is False
    assert body["drive_reachable"] is False
    assert body["error"]
    assert "refresh" in body["error"].lower() or "refus" in body["error"].lower()
