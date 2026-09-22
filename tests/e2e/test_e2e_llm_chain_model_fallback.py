"""Bout en bout : la chaîne LLM survit à un catalogue de hub qui a bougé.

Reproduit l'incident du 2026-09-16 : le hub LiteLLM répond
``400 Invalid model name passed in model=mistral-small-24b`` (et de même
pour ``chat-small``) alors que ``gptoss-120b`` fonctionne. Avant le repli,
quatre étapes sur cinq rendaient vide et le compte-rendu sortait
« partiellement » sans nettoyage ni reformulation.

Le hub est un vrai serveur HTTP local ; le client et les étapes de
meeting-intelligence sont le code de production, chargé depuis ses fichiers.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

import pytest

pytest.importorskip("requests")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.e2e._fakes import FakeService  # noqa: E402

BRIDGE_APP = os.path.join(ROOT, "services", "dmz-to-internal-bridge", "app")

_PROD_BODY = ('{{"error":{{"message":"/chat/completions: Invalid model name passed in '
              'model={model}. Call `/v1/models` to view available models for your key.",'
              '"type":"None","param":"None","code":"400","provider_specific_fields":{{}}}}}}')

TRANSCRIPT = (
    "Bonjour à tous, euh, on démarre la réunion de suivi du projet. "
    "Le deux M L F D I nous demande le rapport pour vendredi. "
    "Donc, hein, on valide que Paul prépare la note et que Marie relit."
) * 3


def _load(name: str, filename: str, as_name: str):
    spec = importlib.util.spec_from_file_location(as_name, os.path.join(BRIDGE_APP, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mi():
    """meeting_intelligence + llm_client réels, sous un paquet ``app`` propre."""
    saved = {k: sys.modules.get(k) for k in ("app", "app.llm_client", "app.glossary_loader",
                                              "app.meeting_intelligence", "requests")}
    # Un stub ``requests`` laissé par un test unitaire voisin casserait l'appel réseau.
    if saved["requests"] is not None and not getattr(saved["requests"], "__file__", None):
        sys.modules.pop("requests")
    pkg = types.ModuleType("app")
    pkg.__path__ = [BRIDGE_APP]
    sys.modules["app"] = pkg
    llm_client = _load("llm_client", "llm_client.py", "app.llm_client")
    glossary = _load("glossary_loader", "glossary_loader.py", "app.glossary_loader")
    mod = _load("meeting_intelligence", "meeting_intelligence.py", "app.meeting_intelligence")
    pkg.llm_client, pkg.glossary_loader, pkg.meeting_intelligence = llm_client, glossary, mod
    yield types.SimpleNamespace(mi=mod, llm_client=llm_client)
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


class _Hub:
    """Faux hub LiteLLM : refuse certains noms, sert les autres."""

    def __init__(self, unknown: set[str]):
        self.unknown = unknown

    def __call__(self, req):
        if req.path != "/v1/chat/completions":
            return 404, {"error": "no such route"}
        if req.headers.get("Authorization") != "Bearer sk-e2e":
            return 401, {"error": "bad key"}
        model = req.body.get("model")
        if model in self.unknown:
            return 400, _PROD_BODY.format(model=model)
        wants_json = (req.body.get("response_format") or {}).get("type") == "json_object"
        prompt = req.body["messages"][0]["content"]
        if wants_json:
            if "participants_cites" in prompt:
                content = json.dumps({"resume": f"analyse par {model}",
                                      "decisions": ["Paul prépare la note"],
                                      "actions": [], "participants_presents": [],
                                      "participants_cites": ["Paul", "Marie"]})
            else:
                content = json.dumps({"title": f"Suivi projet ({model})",
                                      "key_points": ["Rapport pour vendredi", "Paul prépare la note"]})
        else:
            content = f"[{model}] " + prompt.split("\n")[-1][:120]
        return 200, {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def hub():
    handler = _Hub(unknown={"mistral-small-24b", "chat-small"})
    svc = FakeService(handler).start()
    yield svc
    svc.stop()


def _models_seen(hub):
    return [r.body.get("model") for r in hub.calls("POST", "/v1/chat/completions")]


def test_chain_recovers_from_unknown_models_with_fallback(mi, hub, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_FALLBACKS", "chat,gptoss-120b")
    mi.llm_client._UNKNOWN_MODELS.clear()
    llm = mi.llm_client.LLMClient(base_url=hub.url, api_key="sk-e2e", timeout=5)

    cleaned = mi.mi.clean_oob(TRANSCRIPT, llm, "mistral-small-24b")
    reformulated = mi.mi.reformulate(TRANSCRIPT, llm, "mistral-small-24b")
    meta = mi.mi.suggest_metadata(TRANSCRIPT, llm, "chat-small")
    analysis = mi.mi.analyse_meeting(TRANSCRIPT, llm, "gptoss-120b")

    # Chaque étape a produit quelque chose — c'est ce qui manquait en prod.
    assert cleaned and cleaned.startswith("[chat]")
    assert reformulated and reformulated.startswith("[chat]")
    assert meta == {"title": "Suivi projet (chat)",
                    "key_points": ["Rapport pour vendredi", "Paul prépare la note"]}
    assert analysis and analysis["resume"] == "analyse par gptoss-120b"

    seen = _models_seen(hub)
    # Le détour 400 est payé UNE fois par nom inconnu, pas à chaque étape.
    assert seen.count("mistral-small-24b") == 1
    assert seen.count("chat-small") == 1
    assert seen[:2] == ["mistral-small-24b", "chat"]
    # Le grand modèle, lui, n'a jamais eu besoin de repli.
    assert seen[-1] == "gptoss-120b"


def test_without_fallback_the_incident_reproduces(mi, hub, monkeypatch):
    """Témoin : sans repli configuré, le comportement d'avant — étapes vides."""
    monkeypatch.setenv("LLM_MODEL_FALLBACKS", "")
    mi.llm_client._UNKNOWN_MODELS.clear()
    llm = mi.llm_client.LLMClient(base_url=hub.url, api_key="sk-e2e", timeout=5)

    assert mi.mi.clean_oob(TRANSCRIPT, llm, "mistral-small-24b") is None
    assert mi.mi.suggest_metadata(TRANSCRIPT, llm, "chat-small") is None
    assert mi.mi.analyse_meeting(TRANSCRIPT, llm, "gptoss-120b") is not None
    assert set(_models_seen(hub)) == {"mistral-small-24b", "chat-small", "gptoss-120b"}


def test_glossary_correction_step_also_recovers(mi, hub, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_FALLBACKS", "chat")
    mi.llm_client._UNKNOWN_MODELS.clear()
    llm = mi.llm_client.LLMClient(base_url=hub.url, api_key="sk-e2e", timeout=5)
    out = mi.mi.apply_glossary_correction(
        TRANSCRIPT, llm, "mistral-small-24b",
        # Le filtre de pertinence garde un terme si son sigle est épelé
        # lettre à lettre dans le transcript (« M L F D I ») ; « DSI »
        # n'apparaît pas et doit être écarté.
        glossary_terms=["MLFDI", "DSI"],
    )
    assert out and out.startswith("[chat]")
    assert _models_seen(hub) == ["mistral-small-24b", "chat"]
