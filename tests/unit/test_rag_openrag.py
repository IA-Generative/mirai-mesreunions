"""Sources renvoyées par OpenRAG au widget « Interroger mes réunions ».

OpenRAG passe de 2.2.1 à 2.3.1. Deux choses changent dans la réponse de
``POST /v1/chat/completions`` (champ ``extra``) :

  * en 2.2.1 ``extra`` est une CHAÎNE JSON, en 2.3.1 un OBJET ;
  * en 2.2.1 les métadonnées d'un passage sont au premier niveau de la
    source ; en 2.3.1 elles sont sous ``chunk`` (``_id`` compris). Restent au
    premier niveau : ``source_type``, ``chunk_url``, ``file_url`` (seulement
    si le passage a un champ ``source``) et les scores (``rerank_score``).
    Les sources web ne changent pas.

Le widget doit afficher la MÊME chose dans les deux cas. Ces tests passent
par les vrais points d'entrée : ``_openrag.chat`` (qui lit la réponse
d'OpenRAG) et la route ``POST /api/rag/query`` (qui la joint aux
métadonnées de la partition pour le widget).

  * variantes 2.2.1 : figent ce que l'application produit aujourd'hui ;
  * variantes 2.3.1 : même résultat attendu pour les mêmes données.

Le widget n'appelle jamais OpenRAG en flux (``stream: False``) : le cas SSE
ne le concerne pas, un test le vérifie.

Données fictives : aucun nom, aucun hôte réel.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import types

import pytest

pytest.importorskip("flask")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.unit.test_meeting_prep_route import _load_mesreunions_web, _login  # noqa: E402


HOTE = "https://openrag.example"
PARTITION = "perso-sub-de-test"
SUB = "sub-de-test"

UAF_A = "0b9c6a52-1d2e-4f3a-9b8c-7d6e5f4a3b2c"
UAF_B = "5e4d3c2b-1a09-4f8e-8d7c-6b5a4f3e2d1c"

# Clés que le serveur laisse au premier niveau en 2.3.1 (le reste va sous chunk).
_HAUT = ("source_type", "chunk_url", "file_url", "vector_score", "rerank_score", "combined_score")


# ─── Fabrique de réponses OpenRAG ──────────────────────────────────


def _passage(_id, uaf, page, titre, *, source="youtube", avec_file_url=True, **libres):
    """Une source documentaire en forme 2.2.1 (à plat), telle que la renvoie
    OpenRAG pour un document indexé par ``rag_ingest`` : métadonnées du
    serveur + métadonnées libres posées à l'indexation."""
    s = {
        "title": titre,
        "uaf_id": uaf,
        "meeting_title": titre,
        "origin": "youtube_import",
        "file_id": uaf.replace("-", "_"),   # OpenRAG remplace les tirets du chemin
        "partition": PARTITION,
        "page": page,
        "chunk_type": "text",
        "filename": f"{uaf}.md",
        "original_filename": f"{uaf}.md",
        "file_size": "2.10 KB",
        "indexed_at": "2026-10-01T08:00:00+00:00",
        "_id": _id,
        "source_type": "document",
        "chunk_url": f"{HOTE}/extract/{_id}",
    }
    if source is not None:
        s["source"] = source
    if avec_file_url:
        s["file_url"] = f"{HOTE}/static/{_id}"
    s.update(libres)
    return s


def _source_web():
    return {"source_type": "web", "url": "https://www.example.org/page",
            "title": "Une page web", "snippet": "Extrait de la page…"}


def _vers_231(source):
    """Source 2.2.1 → la même en forme 2.3.1 (métadonnées sous ``chunk``)."""
    if not isinstance(source, dict) or source.get("source_type") == "web":
        return source
    haut = {k: source[k] for k in _HAUT if k in source}
    return {"source_type": source.get("source_type", "document"),
            "chunk": {k: v for k, v in source.items() if k not in _HAUT}, **haut}


def _reponse(sources, forme, *, contenu="Selon la réunion du comité [1]…", **extra_en_plus):
    """Corps de ``/v1/chat/completions`` non streamé, en forme 2.2.1 ou 2.3.1."""
    sources = copy.deepcopy(sources)
    if forme == "2.3.1":
        sources = [_vers_231(s) for s in sources]
    extra = {"sources": sources, "presented_sources": sources,
             "cited_sources": sources[:1], "citations_reported": True, **extra_en_plus}
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "model": f"openrag-{PARTITION}",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": contenu}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "extra": json.dumps(extra) if forme == "2.2.1" else extra,
    }


def _sources_du_jeu():
    """Deux passages d'une même réunion (A, pages 1 et 2, l'un reclassé),
    un passage d'une autre réunion (B) sans ``source`` donc sans ``file_url``."""
    return [
        _passage("1001", UAF_A, 1, "Comité de pilotage budget", rerank_score=0.81),
        _passage("1002", UAF_A, 2, "Comité de pilotage budget"),
        _passage("2001", UAF_B, 1, "Revue de projet", source=None, avec_file_url=False,
                 origin="mcr_import"),
    ]


# Fichiers de la partition (GET /partition/{p}) : identiques en 2.2.1 et 2.3.1.
_FICHIERS_PARTITION = [
    {"file_id": UAF_A.replace("-", "_"),
     "metadata": {"uaf_id": UAF_A, "meeting_title": "Comité de pilotage budget",
                  "original_title": "Comité de pilotage — budget 2027",
                  "source_type": "youtube", "origin": "youtube_import",
                  "channel": "Chaîne de test", "canonical_url": "https://video.example/a",
                  "meeting_datetime": "2026-09-30T10:00:00"}},
    {"file_id": UAF_B.replace("-", "_"),
     "metadata": {"uaf_id": UAF_B, "meeting_title": "Revue de projet",
                  "source_type": "upload", "origin": "mcr_import"}},
]


class _Rep:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FausseOpenRAG:
    """Remplace ``requests`` dans ``_openrag`` : aucune instance n'est appelée."""

    def __init__(self, reponse_chat, fichiers=None, partition_en_panne=False):
        self.reponse_chat = reponse_chat
        self.fichiers = _FICHIERS_PARTITION if fichiers is None else fichiers
        self.partition_en_panne = partition_en_panne
        self.corps_chat = None

    def post(self, url, **kw):
        if url.endswith("/v1/chat/completions"):
            self.corps_chat = kw.get("json")
            return _Rep(200, self.reponse_chat)
        return _Rep(201, {})          # ensure_partition

    def get(self, url, **kw):
        if self.partition_en_panne:
            raise ConnectionError("partition indisponible")
        return _Rep(200, {"files": self.fichiers})


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setenv("OPENRAG_BASE_URL", HOTE)
    monkeypatch.setenv("OPENRAG_API_KEY", "cle-factice")
    mod = _load_mesreunions_web()
    mod.app.config["TESTING"] = True
    openrag = sys.modules["app.modules.rag._openrag"]

    def brancher(fausse):
        monkeypatch.setattr(openrag, "req", types.SimpleNamespace(post=fausse.post, get=fausse.get))
        return fausse

    return mod.app.test_client(), openrag, brancher


def _chat(web, corps):
    _client, openrag, brancher = web
    brancher(_FausseOpenRAG(corps))
    return openrag.chat(PARTITION, "Qu'a-t-on décidé sur le budget ?")


def _interroger(web, corps, **kw):
    client, _openrag, brancher = web
    fausse = brancher(_FausseOpenRAG(corps, **kw))
    _login(client, sub=SUB)
    r = client.post("/api/rag/query", json={"question": "Qu'a-t-on décidé sur le budget ?"})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json(), fausse


FORMES = ["2.2.1", "2.3.1"]


# ─── _openrag.chat : lecture de la réponse ─────────────────────────


def test_chat_221_rend_les_sources_telles_quelles(web):
    """Non-régression : en 2.2.1 les sources sont rendues sans transformation."""
    res = _chat(web, _reponse(_sources_du_jeu(), "2.2.1"))
    assert res["answer"] == "Selon la réunion du comité [1]…"
    assert res["sources"] == _sources_du_jeu()


def test_chat_231_rend_les_memes_sources_qu_en_221(web):
    """En 2.3.1 (``extra`` objet, métadonnées sous ``chunk``) : mêmes sources."""
    attendu = _chat(web, _reponse(_sources_du_jeu(), "2.2.1"))
    res = _chat(web, _reponse(_sources_du_jeu(), "2.3.1"))
    assert res == attendu
    assert [s.get("file_id") for s in res["sources"]] == [
        UAF_A.replace("-", "_"), UAF_A.replace("-", "_"), UAF_B.replace("-", "_")]
    assert [s.get("page") for s in res["sources"]] == [1, 2, 1]
    assert res["sources"][0]["rerank_score"] == 0.81
    assert "file_url" not in res["sources"][2]
    assert all("chunk" not in s for s in res["sources"])


@pytest.mark.parametrize("forme", FORMES)
def test_chat_liste_mixte_document_et_web(web, forme):
    sources = [_sources_du_jeu()[0], _source_web()]
    res = _chat(web, _reponse(sources, forme))
    assert res["sources"] == sources
    assert res["sources"][1] == _source_web()


@pytest.mark.parametrize("extra", [
    pytest.param("__absent__", id="absent"),
    pytest.param(None, id="null"),
    pytest.param("", id="chaine-vide"),
    pytest.param("{}", id="chaine-objet-vide-2.2.1"),
    pytest.param({}, id="objet-vide-2.3.1"),
    pytest.param("pas du json", id="chaine-invalide"),
    pytest.param('{"sources": null}', id="sources-null-2.2.1"),
    pytest.param({"sources": None}, id="sources-null-2.3.1"),
    pytest.param([], id="liste"),
])
def test_chat_extra_vide_ou_illisible_donne_zero_source(web, extra):
    corps = _reponse([], "2.2.1")
    if extra == "__absent__":
        corps.pop("extra")
    else:
        corps["extra"] = extra
    res = _chat(web, corps)
    assert res["answer"] == "Selon la réunion du comité [1]…"
    assert not res["sources"]


def test_chat_source_minimale_sans_page_titre_url_file_url_ni_score(web):
    """Un passage réduit à ce que le serveur garantit."""
    minimale = {"_id": "3001", "file_id": "f-min", "partition": PARTITION, "chunk_type": "text",
                "filename": "f-min.md", "source_type": "document",
                "chunk_url": f"{HOTE}/extract/3001"}
    attendu = _chat(web, _reponse([minimale], "2.2.1"))
    assert attendu["sources"] == [minimale]
    assert _chat(web, _reponse([minimale], "2.3.1")) == attendu


@pytest.mark.parametrize("forme", FORMES)
def test_chat_non_streame_tronque(web, forme):
    """2.3.1 peut poser ``truncated: true`` en non streamé : rien ne casse."""
    res = _chat(web, _reponse(_sources_du_jeu(), forme, contenu="Réponse coupée", truncated=True))
    assert res["answer"] == "Réponse coupée"
    assert res["sources"] == _sources_du_jeu()


def test_chat_n_utilise_pas_le_flux(web):
    """Le cas SSE (``extra`` vide sauf sur le dernier morceau) ne concerne pas
    ce client : il demande toujours une réponse complète."""
    _client, openrag, brancher = web
    fausse = brancher(_FausseOpenRAG(_reponse([], "2.2.1")))
    openrag.chat(PARTITION, "q")
    assert fausse.corps_chat["stream"] is False


# ─── POST /api/rag/query : ce que reçoit le widget ─────────────────


_META_A = {"uaf_id": UAF_A, "meeting_title": "Comité de pilotage budget",
           "original_title": "Comité de pilotage — budget 2027", "source_type": "youtube",
           "origin": "youtube_import", "channel": "Chaîne de test",
           "canonical_url": "https://video.example/a", "meeting_datetime": "2026-09-30T10:00:00"}
_META_B = {"uaf_id": UAF_B, "meeting_title": "Revue de projet",
           "source_type": "upload", "origin": "mcr_import"}


def test_route_221_fige_ce_que_recoit_le_widget(web):
    """Non-régression : la source à plat + ``metadata`` joint par ``file_id``."""
    body, _ = _interroger(web, _reponse(_sources_du_jeu(), "2.2.1"))
    assert body["answer"] == "Selon la réunion du comité [1]…"
    attendu = _sources_du_jeu()
    attendu[0]["metadata"] = _META_A
    attendu[1]["metadata"] = _META_A
    attendu[2]["metadata"] = _META_B
    assert body["sources"] == attendu


def test_route_231_le_widget_recoit_la_meme_chose_qu_en_221(web):
    attendu, _ = _interroger(web, _reponse(_sources_du_jeu(), "2.2.1"))
    body, _ = _interroger(web, _reponse(_sources_du_jeu(), "2.3.1"))
    assert body == attendu
    # Ce que lit le widget : identifiant cliquable, titre, icône.
    assert [s["metadata"]["uaf_id"] for s in body["sources"]] == [UAF_A, UAF_A, UAF_B]
    assert [s["metadata"]["meeting_title"] for s in body["sources"]] == [
        "Comité de pilotage budget", "Comité de pilotage budget", "Revue de projet"]


@pytest.mark.parametrize("forme", FORMES)
def test_route_partition_en_panne_l_uaf_id_retombe_sur_le_file_id(web, forme):
    """Garantie de ``_enrich_sources`` : ``metadata.uaf_id`` toujours renseigné,
    en UUID à tirets, même quand la jointure avec la partition échoue."""
    body, _ = _interroger(web, _reponse(_sources_du_jeu(), forme), partition_en_panne=True)
    assert [s["metadata"] for s in body["sources"]] == [
        {"uaf_id": UAF_A}, {"uaf_id": UAF_A}, {"uaf_id": UAF_B}]


@pytest.mark.parametrize("forme", FORMES)
def test_route_liste_mixte_document_et_web(web, forme):
    body, _ = _interroger(web, _reponse([_sources_du_jeu()[2], _source_web()], forme))
    assert body["sources"][0]["metadata"] == _META_B
    assert body["sources"][0]["page"] == 1
    assert body["sources"][1] == {**_source_web(), "metadata": {"uaf_id": ""}}


@pytest.mark.parametrize("forme", FORMES)
def test_route_tronque_et_sans_score(web, forme):
    sources = [_passage("4001", UAF_B, 3, "Revue de projet", origin="mcr_import")]
    body, _ = _interroger(web, _reponse(sources, forme, truncated=True))
    assert len(body["sources"]) == 1
    s = body["sources"][0]
    assert s["metadata"] == _META_B
    assert s["page"] == 3 and s["chunk_url"] == f"{HOTE}/extract/4001"
    assert s["file_url"] == f"{HOTE}/static/4001"
    assert "rerank_score" not in s
