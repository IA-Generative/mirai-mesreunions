"""Tests du fournisseur « réunions précédentes » (lot B).

Le point sensible n'est pas l'assemblage mais les effets de bord des appels
internes : lire une réunion comme source ne doit pas la faire remonter dans
« consultées récemment », et une réunion illisible ne doit jamais faire
échouer la génération entière.
"""

import importlib.util
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_meeting_prep():
    path = os.path.join(ROOT, "services", "mesreunions-web", "app", "meeting_prep.py")
    spec = importlib.util.spec_from_file_location("meeting_prep_prior_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mp = _load_meeting_prep()


def _drain(provider, budget=10_000):
    """Charge tous les items d'un fournisseur et rend (textes, statuts)."""
    texts, statuses = [], []
    for item in provider.items():
        text, status = item.load(budget)
        texts.append(text)
        statuses.append(status)
    return texts, statuses


def test_fetches_each_selected_meeting():
    seen = []

    def _fetch(prep_id, include):
        seen.append((prep_id, tuple(include)))
        return f"contenu de {prep_id}"

    provider = mp.PriorMeetingProvider(_fetch, [
        {"id": "p1", "label": "Comité du 12/03", "include": ["brief"]},
        {"id": "p2", "label": "Point hebdo", "include": ["brief", "key_points"]},
    ])

    assert provider.count_hint() == 2
    texts, statuses = _drain(provider)
    assert statuses == ["ingested", "ingested"]
    assert texts == ["contenu de p1", "contenu de p2"]
    assert seen == [("p1", ("brief",)), ("p2", ("brief", "key_points"))]


def test_unreachable_meeting_degrades_only_itself():
    """Un appel interne en échec ne doit pas tuer la génération."""
    def _fetch(prep_id, _include):
        if prep_id == "p1":
            raise RuntimeError("DTA injoignable")
        return "contenu de p2"

    provider = mp.PriorMeetingProvider(_fetch, [
        {"id": "p1", "label": "KO"}, {"id": "p2", "label": "OK"},
    ])
    texts, statuses = _drain(provider)
    assert statuses == ["error_internal_api", "ingested"]
    assert texts[1] == "contenu de p2"


def test_empty_meeting_is_marked_not_ingested():
    provider = mp.PriorMeetingProvider(lambda *_a: "   ", [{"id": "p1"}])
    _texts, statuses = _drain(provider)
    assert statuses == ["skipped_empty_source"]


def test_meetings_land_in_their_own_bucket():
    """Séparé de prep_docs : sinon un gros dossier Drive les évincerait."""
    provider = mp.PriorMeetingProvider(lambda *_a: "compte-rendu", [{"id": "p1"}])
    buckets, used = mp.build_corpus([provider], budgets=mp.DEFAULT_BUDGETS)
    assert "prior_meetings" in buckets
    assert "prep_docs" not in buckets
    assert used[0]["origin"] == "preparation"


def test_label_is_used_as_document_name():
    provider = mp.PriorMeetingProvider(
        lambda *_a: "contenu", [{"id": "p1", "label": "Comité du 12/03"}],
    )
    buckets, used = mp.build_corpus([provider], budgets=mp.DEFAULT_BUDGETS)
    assert "Comité du 12/03" in buckets["prior_meetings"]
    assert used[0]["name"] == "Comité du 12/03"


def test_inline_provider_respects_the_budget():
    provider = mp.InlineProvider([{"title": "Mail", "text": "a" * 500}])
    buckets, used = mp.build_corpus([provider], budgets={"inline_messages": 100})
    assert used[0]["status"] == "ingested"
    assert len(buckets["inline_messages"]) < 300
