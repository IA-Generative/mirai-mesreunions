"""Régression « queue bloquée » (incident 2026-06-09).

Symptôme : depuis le déploiement de 9ed6b76 (policy serveur AUTO_TRANSCRIBE,
défaut sûr = off), tout upload arrivait en zone interne avec
``transcription_status = 'disabled'`` et n'était jamais transcrit.

Deux niveaux indépendants doivent rester garantis :

  1. **Câblage publish-side** (ce fichier) : ``process_file_ready`` doit
     propager fidèlement ``token_opt.auto_transcribe`` dans le message
     ``internal_pull`` — True ⇒ True, absent ⇒ False (fail-safe PA-01).
     Si ce contrat casse, le puller pose ``disabled`` et la file se bloque.

  2. **Config de déploiement** : les services émetteurs de jetons
     (device-token-authority, mesreunions-web) doivent tourner avec une
     policy *activante* (``user`` / ``on``). Ce niveau n'est PAS couvrable en
     unitaire (les manifestes prod-bêta vivent hors-git) → cf.
     ``tools/diagnose_blocked_queue.sh`` qui le détecte en live.

La logique pure de ``resolve_auto_transcribe`` est déjà testée dans
``test_auto_transcribe_policy.py`` ; ici on verrouille le point d'usage réel
qui a transformé un « défaut sûr » en panne silencieuse pour 100 % des users.
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _install_stubs():
    os.environ.setdefault("INTERNAL_API_TOKEN", "x" * 48)
    os.environ["INTERNAL_PUSH_TRIGGER_URL"] = ""

    requests_stub = types.ModuleType("requests")
    requests_stub.post = MagicMock(return_value=types.SimpleNamespace(status_code=200))

    class _ReqExc(Exception):
        pass

    requests_stub.RequestException = _ReqExc
    requests_stub.HTTPError = _ReqExc
    sys.modules["requests"] = requests_stub

    qh_stub = types.ModuleType("libs.shared.app.queue_helper")
    qh_stub.consume_queue = MagicMock()
    qh_stub.declare_queues = MagicMock()
    qh_stub.publish_message = MagicMock()
    qh_stub.QUEUE_FILE_READY = "file_ready"
    qh_stub.QUEUE_INTERNAL_PULL = "internal_pull"

    class _RMQ:
        host = "x"; port = 5672; user = "u"; password = "p"; vhost = "/"

    qh_stub.RabbitMQConfig = _RMQ
    sys.modules["libs.shared.app.queue_helper"] = qh_stub

    cfg_stub = types.ModuleType("libs.shared.app.config")
    cfg_stub.load_ext_db = lambda: types.SimpleNamespace(sync_url="sqlite:///:memory:")
    cfg_stub.RabbitMQConfig = _RMQ
    cfg_stub.INTERNAL_API_TOKEN = "x" * 48
    cfg_stub.INTERNAL_PUSH_TRIGGER_URL = ""
    sys.modules["libs.shared.app.config"] = cfg_stub

    models_stub = types.ModuleType("libs.shared.app.models")
    for n in ("ExternalBase", "UploadedFile", "UploadSession", "UploadStatus",
              "UploadTokenOption"):
        setattr(models_stub, n, MagicMock())
    sys.modules["libs.shared.app.models"] = models_stub

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_kw: MagicMock()
    db_stub.init_tables = MagicMock()
    sys.modules["libs.shared.app.database"] = db_stub

    sec_stub = types.ModuleType("libs.shared.app.security")
    sec_stub.require_strong_shared_secret = lambda *_a, **_kw: None

    # Reproduction fidèle de resolve_auto_transcribe (logique pure, lit l'env).
    # process_file_ready l'appelle quand token_opt is None (upload local sans
    # jeton) : la décision suit alors AUTO_TRANSCRIBE_POLICY. La logique réelle
    # est testée dans test_auto_transcribe_policy.py ; ici on teste le câblage.
    def _resolve_auto_transcribe(requested, *, is_admin=False, env=None):
        p = ((env or os.environ).get("AUTO_TRANSCRIBE_POLICY", "off") or "off").strip().lower()
        if p == "user":
            return bool(requested)
        if p == "admin":
            return bool(requested) and bool(is_admin)
        if p == "on":
            return True
        return False

    sec_stub.resolve_auto_transcribe = _resolve_auto_transcribe
    sys.modules["libs.shared.app.security"] = sec_stub

    if "libs.shared.app.trigger_url" in sys.modules:
        del sys.modules["libs.shared.app.trigger_url"]
    spec_t = importlib.util.spec_from_file_location(
        "libs.shared.app.trigger_url",
        os.path.join(ROOT, "libs", "shared", "app", "trigger_url.py"),
    )
    t_mod = importlib.util.module_from_spec(spec_t)
    spec_t.loader.exec_module(t_mod)
    sys.modules["libs.shared.app.trigger_url"] = t_mod


def _load_main_module():
    sys.modules.pop("dmz_bridge_main_aft", None)
    spec = importlib.util.spec_from_file_location(
        "dmz_bridge_main_aft",
        os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wire_db(mod, *, token_opt):
    """Make process_file_ready see a file → session → token_opt chain."""
    file_obj = types.SimpleNamespace(id="fid-1", session_id="sid-1",
                                     status=None, status_message=None)
    session_obj = types.SimpleNamespace(id="sid-1", qr_token="QR-1")

    def _query(model):
        q = MagicMock()
        name = getattr(model, "_mock_name", "") or str(model)
        # Resolve .filter(...).first() to the right row per queried model.
        if model is mod.UploadedFile:
            q.filter.return_value.first.return_value = file_obj
        elif model is mod.UploadSession:
            q.filter.return_value.first.return_value = session_obj
        elif model is mod.UploadTokenOption:
            q.filter.return_value.first.return_value = token_opt
        else:
            q.filter.return_value.first.return_value = None
        return q

    db = MagicMock()
    db.query.side_effect = _query
    mod.create_session_factory = lambda *_a, **_kw: (lambda: db)
    mod.notify_portal = MagicMock()
    captured = {}

    def _capture_publish(msg):
        captured["msg"] = msg
        return True

    mod.publish_internal_pull = _capture_publish
    return file_obj, captured


def test_token_auto_transcribe_true_propagates_true():
    _install_stubs()
    mod = _load_main_module()
    token_opt = types.SimpleNamespace(auto_transcribe=True)
    _file, captured = _wire_db(mod, token_opt=token_opt)

    ok = mod.process_file_ready({"file_id": "fid-1"})

    assert ok is True
    # The single most important assertion: a token that opted in must NOT
    # land the upload in 'disabled'. The puller reads exactly this field.
    assert captured["msg"]["auto_transcribe"] is True


def test_missing_token_option_fail_safe_false_when_policy_off():
    # Upload local (pas de jeton) + policy off/absente ⇒ fail-safe PA-01 :
    # auto_transcribe False. La valeur n'est PLUS codée en dur — elle découle
    # de resolve_auto_transcribe, donc on neutralise explicitement l'env.
    _install_stubs()
    os.environ.pop("AUTO_TRANSCRIBE_POLICY", None)
    mod = _load_main_module()
    _file, captured = _wire_db(mod, token_opt=None)

    ok = mod.process_file_ready({"file_id": "fid-1"})

    assert ok is True
    assert captured["msg"]["auto_transcribe"] is False


def test_missing_token_option_transcribes_when_policy_on():
    # Upload local (pas de jeton) + AUTO_TRANSCRIBE_POLICY=on ⇒ la policy
    # serveur s'applique côté process_file_ready et le fichier est transcrit
    # (Fix B incident 2026-06-11 : sinon tout upload local finit 'disabled').
    _install_stubs()
    os.environ["AUTO_TRANSCRIBE_POLICY"] = "on"
    try:
        mod = _load_main_module()
        _file, captured = _wire_db(mod, token_opt=None)
        ok = mod.process_file_ready({"file_id": "fid-1"})
    finally:
        os.environ.pop("AUTO_TRANSCRIBE_POLICY", None)

    assert ok is True
    assert captured["msg"]["auto_transcribe"] is True


def test_token_auto_transcribe_false_propagates_false():
    _install_stubs()
    mod = _load_main_module()
    token_opt = types.SimpleNamespace(auto_transcribe=False)
    _file, captured = _wire_db(mod, token_opt=token_opt)

    ok = mod.process_file_ready({"file_id": "fid-1"})

    assert ok is True
    assert captured["msg"]["auto_transcribe"] is False
