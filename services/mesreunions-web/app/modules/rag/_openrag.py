"""Client OpenRAG (Linagora) — proxy minimal pour mesreunions-web.

Cible : ``OPENRAG_BASE_URL`` (ex. https://api.openrag-mirai.fake-domain.name),
auth ``Authorization: Bearer {OPENRAG_API_KEY}`` (token admin de service `or-…`).
Contrat vérifié sur l'instance + repo openrag :
  - POST /partition/{name}                      → créer (201) / 409 existe
  - GET  /partition/{name}                      → fichiers indexés
  - POST /indexer/partition/{p}/file/{file_id}  → multipart (file + metadata) → task
  - GET  /indexer/task/{task_id}                → état (QUEUED/RUNNING/SUCCESS/FAILED)
  - POST /v1/chat/completions  model=openrag-{p} → réponse RAG + sources (champ `extra`)

Le scoping par utilisateur (partition ``perso-{user_sub}``) est imposé par l'appelant
(routes.py), jamais par le client web.
"""
from __future__ import annotations

import json
import logging
import os

import requests as req

logger = logging.getLogger("mesreunions_web.rag")

_HTTP_TIMEOUT = int(os.getenv("OPENRAG_HTTP_TIMEOUT", "90"))


def _base() -> str:
    return os.getenv("OPENRAG_BASE_URL", "").rstrip("/")


def _key() -> str:
    return os.getenv("OPENRAG_API_KEY", "")


def configured() -> bool:
    return bool(_base() and _key())


def _headers(content_type: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {_key()}"}
    if content_type:
        h["Content-Type"] = content_type
    return h


def ensure_partition(partition: str) -> bool:
    """Crée la partition si absente. 201 créé / 200 / 409 existe = OK."""
    r = req.post(f"{_base()}/partition/{partition}", headers=_headers(), timeout=30)
    if r.status_code in (200, 201, 409):
        return True
    r.raise_for_status()
    return False


def partition_files(partition: str, limit: int = 1000) -> list:
    """Liste les fichiers indexés d'une partition (vide si 404)."""
    r = req.get(
        f"{_base()}/partition/{partition}",
        headers=_headers(), params={"limit": limit}, timeout=30,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    body = r.json() if r.text else {}
    return body.get("files", []) if isinstance(body, dict) else []


def chat(partition: str, question: str, *, history: list | None = None,
         max_tokens: int = 700) -> dict:
    """Interroge la partition (RAG) via /v1/chat/completions. → {answer, sources}."""
    messages: list = []
    if history:
        # On ne garde que les tours role/content valides (défense).
        for m in history[-8:]:
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content"):
                messages.append({"role": m["role"], "content": str(m["content"])[:4000]})
    messages.append({"role": "user", "content": question})
    body = {
        "model": f"openrag-{partition}",
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = req.post(
        f"{_base()}/v1/chat/completions",
        headers=_headers("application/json"), json=body, timeout=_HTTP_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    try:
        answer = data["choices"][0]["message"]["content"] or ""
    except Exception:
        answer = ""
    sources = []
    extra = data.get("extra")
    if extra:
        try:
            parsed = json.loads(extra) if isinstance(extra, str) else extra
            sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
        except Exception:
            sources = []
    return {"answer": answer, "sources": sources}


def index_text(partition: str, file_id: str, title: str, text: str,
               metadata: dict | None = None) -> dict:
    """Indexe un document texte (markdown) dans la partition.

    POST multipart (file + metadata JSON). Async côté OpenRAG (retourne une
    task). 409 = déjà présent (skip).
    """
    meta = {"title": title}
    if metadata:
        meta.update(metadata)
    url = f"{_base()}/indexer/partition/{partition}/file/{file_id}"

    def _mk():  # requests consomme le file-handle → en reconstruire un par appel
        return ({"file": (f"{file_id}.md", text.encode("utf-8"), "text/markdown")},
                {"metadata": json.dumps(meta, ensure_ascii=False)})

    files, data = _mk()
    r = req.post(url, headers=_headers(), files=files, data=data, timeout=120)
    # Déjà indexé → on REMPLACE (PUT) pour mettre à jour le contenu (enrichi).
    if r.status_code == 409:
        files2, data2 = _mk()
        r = req.put(url, headers=_headers(), files=files2, data=data2, timeout=120)
    if r.status_code == 409:
        return {"status": "exists"}
    if r.status_code in (200, 201, 202):
        body = r.json() if r.text else {}
        return {"status": "queued", "task": body.get("task_status_url")}
    return {"status": "error", "code": r.status_code, "error": (r.text or "")[:200]}
