"""Tests de l'onglet « Agents » de mesreunions-web (contrat d'agents MirAI).

``GET /api/agents`` et ``POST /api/meetings/<id>/agents/<id>/run`` relaient
le jeton d'ACCÈS de la personne à Mes agents, côté serveur. On simule ici
Mes agents (``requests.request`` du module), Keycloak (le rafraîchissement
silencieux), device-token-authority (la réunion et ``/amend``) et
internal-ingester (le texte de la réunion), pour vérifier : la fonction
désactivée, le relais du jeton, le nouvel essai après 401, l'encadrement
``<<< >>>`` et sa neutralisation, la borne de 20 000 caractères, la
mémorisation dans ``content.agents``, les erreurs du contrat et le refus
d'une réunion d'un autre compte.
"""

import importlib.util
import json
import os
import sys
import types
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest

pytest.importorskip("flask")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_INTERNAL_TOKEN = "not-a-real-credential-synthetic-fixture-value"
MESAGENTS = "http://mesagents.audio-internal.svc.test"
MEETING = "7a0c0000-0000-4000-8000-0000000000b1"
AUTRE_MEETING = "7a0c0000-0000-4000-8000-0000000000b2"
UAF = "5e5e0000-0000-4000-8000-0000000000a1"
FILE_ID = "1f1f0000-0000-4000-8000-0000000000c1"


def _load_web():
    """Charge ``main.py`` avec les effets de bord du boot neutralisés
    (même geste que ``test_web_search_api.py``)."""
    os.environ["INTERNAL_API_TOKEN"] = _INTERNAL_TOKEN
    os.environ.setdefault("SECRET_KEY", "test-secret-key-32-bytes-long-xxxxx")
    os.environ.setdefault("OIDC_ISSUER", "https://kc.test/realms/test")
    os.environ.setdefault("OIDC_CLIENT_ID", "test-client")
    os.environ.setdefault("OIDC_CLIENT_SECRET", "test-secret")
    os.environ.setdefault("OIDC_REDIRECT_URI", "http://test/auth/callback")

    for name in list(sys.modules):
        if name.startswith("libs.shared.app") or name in {"libs.shared", "libs"}:
            stub = sys.modules.get(name)
            if stub is not None and not getattr(stub, "__file__", None):
                sys.modules.pop(name, None)
    for name in ("requests", "authlib", "authlib.integrations",
                 "authlib.integrations.flask_client"):
        stub = sys.modules.get(name)
        if stub is not None and not getattr(stub, "__file__", None):
            sys.modules.pop(name, None)

    web_dir = os.path.join(ROOT, "services", "mesreunions-web")
    while web_dir in sys.path:
        sys.path.remove(web_dir)
    sys.path.insert(0, web_dir)
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        sys.modules.pop(name, None)

    if "pika" not in sys.modules:
        pika = types.ModuleType("pika")
        pika.BlockingConnection = MagicMock()
        pika.ConnectionParameters = MagicMock()
        pika.PlainCredentials = MagicMock()
        pika.exceptions = types.SimpleNamespace(
            AMQPConnectionError=Exception, ChannelClosedByBroker=Exception)
        sys.modules["pika"] = pika
    if "qrcode" not in sys.modules:
        qr = types.ModuleType("qrcode")
        qr.QRCode = MagicMock()
        consts = types.SimpleNamespace(ERROR_CORRECT_M=0)
        qr.constants = consts
        sys.modules["qrcode"] = qr
        sys.modules["qrcode.constants"] = consts

    db_stub = types.ModuleType("libs.shared.app.database")
    db_stub.create_session_factory = lambda *_a, **_k: MagicMock()
    db_stub.init_tables = MagicMock()
    db_stub.with_db_retry = lambda fn, **_k: fn()
    db_stub.__file__ = "<stub>"
    sys.modules["libs.shared.app.database"] = db_stub

    sys.modules.pop("mesreunions_web_agents_api_test", None)
    spec = importlib.util.spec_from_file_location(
        "mesreunions_web_agents_api_test",
        os.path.join(web_dir, "app", "main.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── Faux services ──────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = dict(headers or {})
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("pas de JSON")
        return self._body


def _agent(**over):
    base = {
        "id": "ag-1", "name": "Rédacteur de notes", "description": "Rédige une note <b>courte</b>.",
        "origin": "mine", "categories": [], "tags": [], "version": 1, "visibility": "private",
        "status": "published", "inputs": ["meeting", "text"], "outputs": ["text"],
        "greeting": "", "examples": [], "model": "ag-1-modele",
        "updated_at": "2026-10-06T10:12:00+00:00", "url": "https://agents.test/catalog/ag-1",
    }
    base.update(over)
    return base


def _list_body(*agents):
    agents = list(agents) or [_agent(), _agent(id="ag-2", name="Synthèse", origin="shared",
                                                 model="ag-2", visibility="ministry")]
    return {"source": "mesagents", "total": len(agents), "agents": agents}


def _completion(text="Voici la note."):
    return {"id": "chatcmpl-1", "object": "chat.completion", "model": "ag-1-modele",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}]}


class FakeMesAgents:
    """Faux Mes agents : réponses scénarisées par (méthode, chemin), consommées
    dans l'ordre ; la dernière se répète. Une exception scénarisée est levée."""

    def __init__(self):
        self.calls = []
        self.scripts = {}

    def script(self, method, path, *responses):
        self.scripts[(method, path)] = list(responses)

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        parts = urlsplit(url)
        self.calls.append({"method": method, "url": url, "path": parts.path, "params": params,
                           "json": json, "headers": headers, "timeout": timeout})
        queue = self.scripts.get((method, parts.path))
        if not queue:
            raise AssertionError(f"appel non scénarisé : {method} {parts.path}")
        r = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(r, Exception):
            raise r
        return r

    def runs(self):
        return [c for c in self.calls if c["path"] == "/v1/chat/completions"]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("MESAGENTS_BASE_URL", MESAGENTS + "/")
    monkeypatch.setenv("MESAGENTS_TIMEOUT_SECONDS", "90")
    mod = _load_web()
    mod.app.config["TESTING"] = True

    from app.modules.agents import routes
    from app.modules.auth import token_store, user_token

    fake = FakeMesAgents()
    fake.script("GET", "/api/v1/agents", _Resp(200, _list_body()))
    fake.script("POST", "/v1/chat/completions", _Resp(200, _completion()))
    monkeypatch.setattr(routes.req, "request", fake)

    # Dépôt de jetons côté serveur (web_session_tokens).
    tokens = {"access_token": "AT1", "refresh_token": "RT1"}
    monkeypatch.setattr(token_store, "load_tokens", lambda ref=None: dict(tokens))

    def _update(ref=None, *, access_token=None, refresh_token=None):
        if access_token:
            tokens["access_token"] = access_token
        if refresh_token:
            tokens["refresh_token"] = refresh_token
        return True
    monkeypatch.setattr(token_store, "update_tokens", _update)

    # Keycloak : le rafraîchissement silencieux.
    keycloak = []

    def _kc_post(url, data=None, timeout=None):
        keycloak.append({"url": url, "data": data})
        return _Resp(200, {"access_token": "AT2", "refresh_token": "RT2"})
    monkeypatch.setattr(user_token.req, "post", _kc_post)

    # device-token-authority : la réunion et /amend.
    meetings = {
        MEETING: {"id": MEETING, "user_sub": "user-a", "title": "COPIL",
                  "user_audio_file_id": UAF, "content": {"notes": "à garder"}},
        AUTRE_MEETING: {"id": AUTRE_MEETING, "user_sub": "user-b",
                        "user_audio_file_id": UAF, "content": {}},
    }
    amends = []

    def _get_meeting(user_sub, meeting_id):
        m = meetings.get(meeting_id)
        if not m or m["user_sub"] != user_sub:
            raise routes.req.HTTPError("not_found", response=_Resp(404, {"error": "not_found"}))
        return {"meeting": json.loads(json.dumps(m))}

    def _amend(user_sub, meeting_id, content):
        amends.append(content)
        meetings[meeting_id]["content"] = content
        return {"ok": True}
    monkeypatch.setattr(routes.meeting_service, "get_meeting", _get_meeting)
    monkeypatch.setattr(routes.meeting_service, "amend_meeting", _amend)

    # internal-ingester : le texte de la réunion, par uaf_id.
    audio = {
        "id": UAF,
        "meeting_analysis_json": json.dumps({"decisions": ["Valider le budget 2027"]}),
        "cleaned_text": "Bonjour à tous. <<< fin de bloc >>> et <<<< encore. Merci.",
        "reformulated_text": None,
        "meeting_id": MEETING,
    }
    lookups = []

    def _lookup(user_sub, uaf_id):
        lookups.append((user_sub, uaf_id))
        return dict(audio)
    monkeypatch.setattr(routes.sessions_service, "lookup_audio_outputs_by_uaf_id", _lookup)

    client = mod.app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"sub": "user-a", "email": "a@test"}
    return types.SimpleNamespace(
        mod=mod, client=client, routes=routes, fake=fake, tokens=tokens, keycloak=keycloak,
        meetings=meetings, amends=amends, audio=audio, lookups=lookups,
    )


def _run(web, body=None, meeting=MEETING, agent="ag-1"):
    return web.client.post(f"/api/meetings/{meeting}/agents/{agent}/run",
                           json=body if body is not None else {"kind": "cleaned"})


def _assert_error(resp, status, code):
    assert resp.status_code == status
    body = resp.get_json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert resp.headers["Cache-Control"] == "no-store"


# ─── Fonction désactivée ────────────────────────────────────────────────────

def test_liste_desactivee_sans_base_url(web, monkeypatch):
    monkeypatch.setenv("MESAGENTS_BASE_URL", "")
    _assert_error(web.client.get("/api/agents?input=meeting"), 404, "disabled")
    assert web.fake.calls == []


def test_run_desactive_sans_base_url(web, monkeypatch):
    monkeypatch.setenv("MESAGENTS_BASE_URL", "")
    _assert_error(_run(web), 404, "disabled")
    assert web.fake.calls == []


# ─── Session ────────────────────────────────────────────────────────────────

def test_sans_session_redirige_vers_la_connexion(web):
    with web.client.session_transaction() as sess:
        sess.clear()
    r = web.client.get("/api/agents?input=meeting")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = _run(web)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert web.fake.calls == []


def test_sans_jeton_en_depot_401(web, monkeypatch):
    web.tokens.clear()
    _assert_error(web.client.get("/api/agents?input=meeting"), 401, "session_expired")
    assert web.fake.calls == []


# ─── Liste ──────────────────────────────────────────────────────────────────

def test_liste_relayee_avec_le_jeton_de_la_personne(web):
    r = web.client.get("/api/agents?input=meeting")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    call = web.fake.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == f"{MESAGENTS}/api/v1/agents"
    assert call["params"] == {"input": "meeting"}
    assert call["headers"]["Authorization"] == "Bearer AT1"
    # La liste du contrat, telle quelle : la description reste une donnée.
    body = r.get_json()
    assert body["source"] == "mesagents" and body["total"] == 2
    assert body["agents"][0]["description"] == "Rédige une note <b>courte</b>."
    assert body["agents"][1]["origin"] == "shared"
    assert web.keycloak == []


def test_liste_input_par_defaut_meeting(web):
    assert web.client.get("/api/agents").status_code == 200
    assert web.fake.calls[0]["params"] == {"input": "meeting"}


def test_liste_input_hors_vocabulaire_400(web):
    _assert_error(web.client.get("/api/agents?input=javascript"), 400, "invalid_query")
    assert web.fake.calls == []


def test_liste_401_rafraichit_puis_rejoue(web):
    web.fake.script("GET", "/api/v1/agents",
                    _Resp(401, {"error": {"code": "invalid_token", "message": "expiré"}}),
                    _Resp(200, _list_body()))
    r = web.client.get("/api/agents?input=meeting")
    assert r.status_code == 200
    assert len(web.keycloak) == 1
    assert web.keycloak[0]["data"]["grant_type"] == "refresh_token"
    assert web.keycloak[0]["data"]["refresh_token"] == "RT1"
    assert web.keycloak[0]["url"].endswith("/protocol/openid-connect/token")
    auths = [c["headers"]["Authorization"] for c in web.fake.calls]
    assert auths == ["Bearer AT1", "Bearer AT2"]
    assert web.tokens["refresh_token"] == "RT2"


def test_liste_401_persistant_503_forbidden_un_seul_refresh(web):
    web.fake.script("GET", "/api/v1/agents",
                    _Resp(401, {"error": {"code": "invalid_token", "message": "x"}}))
    _assert_error(web.client.get("/api/agents?input=meeting"), 503, "mesagents_forbidden")
    assert len(web.keycloak) == 1
    assert len(web.fake.calls) == 2


def test_liste_403_audience_503_forbidden(web):
    web.fake.script("GET", "/api/v1/agents",
                    _Resp(403, {"error": {"code": "audience_mismatch", "message": "x"}}))
    _assert_error(web.client.get("/api/agents?input=meeting"), 503, "mesagents_forbidden")
    assert web.keycloak == []


def test_liste_injoignable_502(web):
    web.fake.script("GET", "/api/v1/agents", web.routes.req.ConnectionError("down"))
    _assert_error(web.client.get("/api/agents?input=meeting"), 502, "mesagents_unavailable")


def test_liste_5xx_et_reponse_illisible_502(web):
    web.fake.script("GET", "/api/v1/agents", _Resp(500, {"error": "boom"}))
    _assert_error(web.client.get("/api/agents?input=meeting"), 502, "mesagents_unavailable")
    web.fake.script("GET", "/api/v1/agents", _Resp(200, {"pas": "la liste"}))
    _assert_error(web.client.get("/api/agents?input=meeting"), 502, "mesagents_unavailable")


def test_liste_429_relaye_retry_after(web):
    web.fake.script("GET", "/api/v1/agents",
                    _Resp(429, {"error": {"code": "rate_limited", "message": "x"}},
                          headers={"Retry-After": "30"}))
    r = web.client.get("/api/agents?input=meeting")
    _assert_error(r, 429, "rate_limited")
    assert r.headers["Retry-After"] == "30"


# ─── Lancement ──────────────────────────────────────────────────────────────

def test_run_encadre_le_texte_et_neutralise_les_marqueurs(web):
    r = _run(web, {"kind": "cleaned", "instruction": "Résume en trois points."})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["agent"] == {"id": "ag-1", "name": "Rédacteur de notes"}
    assert body["kind"] == "cleaned"
    assert body["output"] == "Voici la note."
    assert body["ran_at"].startswith("20")
    assert body["input_truncated"] is False and body["saved"] is True

    run = web.fake.runs()[0]
    assert run["headers"]["Authorization"] == "Bearer AT1"
    assert run["timeout"] == 90.0
    sent = run["json"]
    assert sent["model"] == "ag-1-modele"       # ``model`` de la fiche, pas supposé égal à l'id
    assert sent["stream"] is False
    assert [m["role"] for m in sent["messages"]] == ["user"]
    content = sent["messages"][0]["content"]
    assert content.startswith("Résume en trois points.\n\n<<<\n")
    assert content.endswith("\n>>>")
    inner = content[len("Résume en trois points.\n\n<<<\n"):-len("\n>>>")]
    assert "<<<" not in inner and ">>>" not in inner
    assert "< < <" in inner and "> > >" in inner and "< < < <" in inner
    assert "Bonjour à tous." in inner and "Merci." in inner
    # Le texte est lu par l'identifiant interne de l'audio de la réunion.
    assert web.lookups == [("user-a", UAF)] or web.lookups[0] == ("user-a", UAF)


def test_run_consigne_par_defaut_et_compte_rendu_en_markdown(web):
    r = _run(web, {"kind": "meeting_analysis"})
    assert r.status_code == 200
    content = web.fake.runs()[0]["json"]["messages"][0]["content"]
    assert content.startswith("Travaille sur le compte rendu de réunion suivant.\n\n<<<\n")
    assert "## Décisions" in content and "Valider le budget 2027" in content


def test_run_memorise_la_derniere_execution_par_agent(web):
    assert _run(web, {"kind": "cleaned", "instruction": "Une consigne"}).status_code == 200
    content = web.meetings[MEETING]["content"]
    assert content["notes"] == "à garder"            # le reste du contenu survit
    assert len(content["agents"]) == 1
    entry = content["agents"][0]
    assert entry["id"] == "ag-1" and entry["name"] == "Rédacteur de notes"
    assert entry["kind"] == "cleaned" and entry["output"] == "Voici la note."
    assert entry["instruction"] == "Une consigne" and entry["ran_at"]
    # Une seconde exécution du même agent remplace la première.
    web.fake.script("POST", "/v1/chat/completions", _Resp(200, _completion("Seconde note.")))
    assert _run(web, {"kind": "meeting_analysis"}).status_code == 200
    runs = web.meetings[MEETING]["content"]["agents"]
    assert [r["output"] for r in runs] == ["Seconde note."]
    assert runs[0]["kind"] == "meeting_analysis" and runs[0]["instruction"] is None
    # Un autre agent s'ajoute devant.
    assert _run(web, {"kind": "cleaned"}, agent="ag-2").status_code == 200
    assert [r["id"] for r in web.meetings[MEETING]["content"]["agents"]] == ["ag-2", "ag-1"]


def test_run_memorisation_bornee_a_dix_et_sortie_tronquee(web):
    web.meetings[MEETING]["content"]["agents"] = [
        {"id": f"vieux-{i}", "name": "x", "kind": "cleaned", "output": "o", "ran_at": "2026"}
        for i in range(12)
    ]
    long_output = "x" * 25_000
    web.fake.script("POST", "/v1/chat/completions", _Resp(200, _completion(long_output)))
    r = _run(web)
    assert r.status_code == 200
    assert len(r.get_json()["output"]) == 25_000      # la personne reçoit tout
    runs = web.meetings[MEETING]["content"]["agents"]
    assert len(runs) == 10
    assert runs[0]["id"] == "ag-1" and len(runs[0]["output"]) == 20_000
    assert [r["id"] for r in runs[1:]] == [f"vieux-{i}" for i in range(9)]


def test_run_borne_le_message_a_20000_caracteres(web):
    web.audio["cleaned_text"] = "mot " * 10_000      # 40 000 caractères
    r = _run(web, {"kind": "cleaned"})
    assert r.status_code == 200
    assert r.get_json()["input_truncated"] is True
    content = web.fake.runs()[0]["json"]["messages"][0]["content"]
    assert len(content) <= 20_000
    assert content.endswith("texte tronqué : la réunion dépasse la taille acceptée par l'agent …]\n>>>")
    assert content.startswith("Travaille sur la transcription de réunion suivante.\n\n<<<\n")


def test_run_ne_bloque_pas_si_la_memorisation_echoue(web, monkeypatch):
    def _boom(user_sub, meeting_id, content):
        raise web.routes.req.HTTPError("amend_failed", response=_Resp(500, {"error": "x"}))
    monkeypatch.setattr(web.routes.meeting_service, "amend_meeting", _boom)
    r = _run(web)
    assert r.status_code == 200
    assert r.get_json()["output"] == "Voici la note." and r.get_json()["saved"] is False


def test_run_401_rafraichit_puis_rejoue(web):
    web.fake.script("POST", "/v1/chat/completions",
                    _Resp(401, {"error": {"message": "expiré", "type": "auth", "code": "invalid_token"}}),
                    _Resp(200, _completion()))
    assert _run(web).status_code == 200
    assert [c["headers"]["Authorization"] for c in web.fake.runs()] == ["Bearer AT1", "Bearer AT2"]
    assert len(web.keycloak) == 1


# ─── Erreurs du contrat ─────────────────────────────────────────────────────

def test_run_422_relaye_le_code_et_le_message_tels_quels(web):
    web.fake.script("POST", "/v1/chat/completions",
                    _Resp(422, {"error": {"message": "Votre demande contient une consigne que "
                                                     "l'agent ne peut pas suivre.",
                                          "type": "invalid_request_error",
                                          "code": "blocked_input"}}))
    r = _run(web)
    assert r.status_code == 422
    assert r.get_json()["error"] == {
        "code": "blocked_input",
        "message": "Votre demande contient une consigne que l'agent ne peut pas suivre.",
    }
    assert "agents" not in web.meetings[MEETING]["content"]


def test_run_agent_absent_de_la_liste_404_sans_appel(web):
    _assert_error(_run(web, agent="inconnu"), 404, "agent_not_found")
    assert web.fake.runs() == []


def test_run_model_not_found_404(web):
    web.fake.script("POST", "/v1/chat/completions",
                    _Resp(404, {"error": {"message": "x", "type": "x", "code": "model_not_found"}}))
    _assert_error(_run(web), 404, "agent_not_found")


def test_run_429_relaye_retry_after(web):
    web.fake.script("POST", "/v1/chat/completions",
                    _Resp(429, {"error": {"message": "x", "type": "x", "code": "rate_limited"}},
                          headers={"Retry-After": "12"}))
    r = _run(web)
    _assert_error(r, 429, "rate_limited")
    assert r.headers["Retry-After"] == "12"


def test_run_5xx_injoignable_et_reponse_vide_502(web):
    web.fake.script("POST", "/v1/chat/completions",
                    _Resp(502, {"error": {"message": "x", "type": "x", "code": "llm_unavailable"}}))
    _assert_error(_run(web), 502, "mesagents_unavailable")
    web.fake.script("POST", "/v1/chat/completions", web.routes.req.ReadTimeout("lent"))
    _assert_error(_run(web), 502, "mesagents_unavailable")
    web.fake.script("POST", "/v1/chat/completions", _Resp(200, {"choices": []}))
    _assert_error(_run(web), 502, "mesagents_unavailable")
    assert "agents" not in web.meetings[MEETING]["content"]


def test_run_403_503_forbidden(web):
    web.fake.script("POST", "/v1/chat/completions",
                    _Resp(403, {"error": {"message": "x", "type": "x", "code": "forbidden"}}))
    _assert_error(_run(web), 503, "mesagents_forbidden")


def test_run_reunion_d_un_autre_compte_refusee(web):
    _assert_error(_run(web, meeting=AUTRE_MEETING), 404, "meeting_not_found")
    _assert_error(_run(web, meeting="inexistante"), 404, "meeting_not_found")
    assert web.fake.calls == [] and web.lookups == []


def test_run_texte_absent_409(web):
    _assert_error(_run(web, {"kind": "reformulated"}), 409, "text_unavailable")
    assert web.fake.runs() == []


def test_run_sans_audio_409(web):
    web.meetings[MEETING]["user_audio_file_id"] = None
    _assert_error(_run(web), 409, "text_unavailable")
    assert web.lookups == []


@pytest.mark.parametrize("body", [
    None, [], {"kind": "speaker_tagged"}, {"kind": "cleaned", "instruction": 12},
    {"kind": "cleaned", "instruction": "x" * 2001},
])
def test_run_corps_invalide_400(web, body):
    r = web.client.post(f"/api/meetings/{MEETING}/agents/ag-1/run", json=body)
    _assert_error(r, 400, "invalid_query")
    assert web.fake.calls == []


def test_run_identifiant_d_agent_invalide_400(web):
    r = web.client.post(f"/api/meetings/{MEETING}/agents/{'a' * 201}/run", json={"kind": "cleaned"})
    _assert_error(r, 400, "invalid_query")
    r = web.client.post(f"/api/meetings/{MEETING}/agents/%3Cscript%3E/run", json={"kind": "cleaned"})
    assert r.status_code in (400, 404)
    assert web.fake.calls == []


# ─── Helpers purs ───────────────────────────────────────────────────────────

def test_neutralisation_des_marqueurs(web):
    n = web.routes.neutralize_markers
    assert n("a <<< b >>> c") == "a < < < b > > > c"
    assert n("<<<<<<") == "< < < < < <"
    assert "<<<" not in n("x<<<<<<<y") and ">>>" not in n("x>>>>y")
    assert n("a << b >> c") == "a << b >> c"


# ─── Portée OIDC et fiche ───────────────────────────────────────────────────

def test_portee_mesagents_demandee_a_la_connexion_quand_active(monkeypatch):
    from libs.shared.app import config
    monkeypatch.setattr(config, "MESAGENTS_BASE_URL", MESAGENTS)
    monkeypatch.setattr(config, "MESAGENTS_OIDC_SCOPE", "mesagents-agents")
    mod = _load_web()
    assert mod._OIDC_SCOPE.split() == ["openid", "email", "profile", "mesagents-agents"]
    r = mod.app.test_client().get("/login")
    assert r.status_code == 302
    assert "scope=openid+email+profile+mesagents-agents" in r.headers["Location"]


def test_portee_mesagents_absente_quand_desactivee(monkeypatch):
    from libs.shared.app import config
    monkeypatch.setattr(config, "MESAGENTS_BASE_URL", "")
    mod = _load_web()
    assert "mesagents" not in mod._OIDC_SCOPE
    r = mod.app.test_client().get("/login")
    assert "mesagents" not in r.headers["Location"]


def test_transcript_status_expose_meeting_id(web, monkeypatch):
    """La fiche (clé ``file_id``) doit connaître sa réunion pour lancer un agent."""
    from app.modules.sessions import routes as sessions_routes
    audio = dict(web.audio, transcription_status="kevent_completed")
    monkeypatch.setattr(sessions_routes, "_audio_or_404",
                        lambda db, sub, fid: (types.SimpleNamespace(id=fid), audio))
    r = web.client.get(f"/api/file/transcript-status/{FILE_ID}?summary=1")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True and body["meeting_id"] == MEETING
