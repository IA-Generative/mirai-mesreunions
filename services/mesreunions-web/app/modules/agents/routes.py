"""Blueprint ``agents`` — les agents MirAI sur une réunion (contrat d'agents).

Contrat : ``docs/contrats/contrat-agents-mirai.md`` du dépôt de Mes agents.
mesreunions-web est un consommateur à client confidentiel : il relaie le
jeton d'ACCÈS de la personne (dépôt ``web_session_tokens``), le rafraîchit
silencieusement et rejoue l'appel une fois sur 401 — même patron que l'import
YouTube (``modules/auth/user_token.py``). Mes agents applique les droits.

  GET  /api/agents?input=meeting
       Relaie ``GET {MESAGENTS_BASE_URL}/api/v1/agents?input=meeting`` et rend
       la liste du contrat telle quelle (texte brut : le navigateur l'affiche
       par ``textContent``).
  POST /api/meetings/<meeting_id>/agents/<agent_id>/run
       Corps ``{"kind": "meeting_analysis"|"cleaned"|"reformulated",
       "instruction": "<facultative>"}``. Vérifie que la réunion appartient à
       la personne, lit le texte demandé par l'ingester, appelle
       ``POST {MESAGENTS_BASE_URL}/v1/chat/completions`` avec l'identifiant de
       l'agent dans ``model``, puis mémorise la dernière exécution par agent
       dans ``Meeting.content["agents"]`` (bornée à 10 entrées).

Erreurs (corps ``{"error": {"code", "message"}}``) :
  404 disabled                 MESAGENTS_BASE_URL vide (l'onglet se cache)
  503 mesagents_forbidden      Mes agents refuse le jeton (401 après refresh, 403)
  502 mesagents_unavailable    injoignable, 5xx, réponse illisible
  429 rate_limited             relayé avec ``Retry-After``
  404 agent_not_found          agent absent de la liste ou ``model_not_found``
  422 <code de Mes agents>     ``blocked_input`` / ``blocked_output`` — ``message``
                               est à afficher tel quel à la personne
  404 meeting_not_found        réunion d'une autre personne, ou inexistante
  409 text_unavailable         le texte demandé n'existe pas (encore)
  400 invalid_query            corps ou identifiants invalides

Rien du texte de la réunion ni de la réponse de l'agent n'est journalisé ;
toutes les réponses portent ``Cache-Control: no-store``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

import requests as req
from flask import Blueprint, jsonify, request

from app.shared import get_current_user, require_auth
from app.modules.auth import user_token
from app.modules.meetings import service as meeting_service
from app.modules.sessions import service as sessions_service

logger = logging.getLogger("mesreunions_web.agents")

bp = Blueprint("agents", __name__)

# Vocabulaire fermé du contrat pour ``inputs`` : on ne relaie jamais une
# valeur hors liste (400 invalid_query côté Mes agents, inutile de l'appeler).
CONTRACT_INPUTS = frozenset({"text", "selection", "document", "email", "thread",
                             "meeting", "collection", "page"})
DEFAULT_INPUT = "meeting"

# Textes qu'un agent peut recevoir → colonne renvoyée par ``/api/v1/audio/lookup``.
RUN_KINDS = {
    "meeting_analysis": "meeting_analysis_json",
    "cleaned": "cleaned_text",
    "reformulated": "reformulated_text",
}
DEFAULT_INSTRUCTIONS = {
    "meeting_analysis": "Travaille sur le compte rendu de réunion suivant.",
    "cleaned": "Travaille sur la transcription de réunion suivante.",
    "reformulated": "Travaille sur la transcription de réunion suivante.",
}

# Bornes du contrat et de la mémorisation. Mes agents accepte 120 000
# caractères par message (422 au-delà) ; le contrat demande au consommateur
# de tronquer au-delà de 100 000 en le disant à la personne — un contenu qui
# dépasse la fenêtre du modèle donne 502 llm_unavailable.
MAX_MESSAGE_CHARS = 100_000
MAX_INSTRUCTION_CHARS = 2_000
MAX_AGENT_ID_CHARS = 200
MAX_STORED_OUTPUT_CHARS = 20_000
MAX_STORED_RUNS = 10
TRUNCATION_NOTE = "\n[… texte tronqué : la réunion dépasse la taille acceptée par l'agent …]"

_OPEN, _CLOSE = "<<<", ">>>"
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class _AgentsError(Exception):
    def __init__(self, status: int, code: str, message: str, headers=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def _error(status: int, code: str, message: str, headers=None):
    resp = jsonify({"error": {"code": code, "message": message}})
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


@bp.after_request
def _no_store(resp):
    # Liste personnelle et textes de réunion : jamais en cache.
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ─── Configuration ──────────────────────────────────────────────────────────

def _base_url() -> str:
    # Lue à chaque requête (comme les variables du module ``search``) : les
    # tests basculent la fonction sans recharger l'application.
    return (os.getenv("MESAGENTS_BASE_URL") or "").strip().rstrip("/")


def _timeout() -> float:
    try:
        return max(1.0, float(os.getenv("MESAGENTS_TIMEOUT_SECONDS") or 120))
    except ValueError:
        return 120.0


def _require_enabled() -> str:
    base = _base_url()
    if not base:
        raise _AgentsError(404, "disabled", "Les agents MirAI ne sont pas activés ici.")
    return base


# ─── Appel à Mes agents avec le jeton de la personne ────────────────────────

def _call(method: str, path: str, *, params=None, json_body=None,
          timeout: float | None = None, _retry: bool = True) -> req.Response:
    """Appel relayé avec le Bearer de la personne.

    Sur 401, UN rafraîchissement silencieux puis un nouvel essai. Les erreurs
    réseau deviennent ``502 mesagents_unavailable`` ; le reste est rendu à
    l'appelant qui l'interprète selon la route du contrat.
    """
    token = user_token.access_token()
    if not token:
        raise _AgentsError(401, "session_expired", "Session expirée, reconnexion requise.")
    try:
        resp = req.request(
            method, f"{_base_url()}{path}",
            params=params, json=json_body,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout if timeout is not None else _timeout(),
        )
    except req.RequestException as exc:
        # Jamais ``exc`` en clair : il peut porter l'URL interne ou des entêtes.
        logger.warning("agents: Mes agents injoignable (%s)", type(exc).__name__)
        raise _AgentsError(502, "mesagents_unavailable", "Mes agents ne répond pas.")
    if resp.status_code == 401 and _retry and user_token.refresh_access_token():
        return _call(method, path, params=params, json_body=json_body,
                     timeout=timeout, _retry=False)
    return resp


def _json(resp: req.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


def _upstream_error(resp: req.Response) -> tuple[str, str]:
    """``(code, message)`` d'une erreur du contrat, les deux formats confondus."""
    err = _json(resp).get("error")
    if not isinstance(err, dict):
        return "", ""
    return str(err.get("code") or ""), str(err.get("message") or "")


def _retry_after(resp: req.Response) -> dict:
    ra = resp.headers.get("Retry-After")
    return {"Retry-After": ra} if ra else {}


def _raise_common(resp: req.Response, what: str) -> None:
    """Statuts communs aux deux routes du contrat."""
    if resp.status_code in (401, 403):
        logger.warning("agents: %s refusé par Mes agents (HTTP %d)", what, resp.status_code)
        raise _AgentsError(503, "mesagents_forbidden",
                           "Mes agents refuse l'accès : portée SSO ou droits à vérifier.")
    if resp.status_code == 429:
        raise _AgentsError(429, "rate_limited", "Trop de demandes, réessayez dans un instant.",
                           headers=_retry_after(resp))


def _list_agents(input_kind: str) -> dict:
    resp = _call("GET", "/api/v1/agents", params={"input": input_kind}, timeout=15)
    _raise_common(resp, "la liste")
    if resp.status_code != 200:
        logger.warning("agents: liste HTTP %d", resp.status_code)
        raise _AgentsError(502, "mesagents_unavailable", "Mes agents ne répond pas.")
    body = _json(resp)
    if not isinstance(body.get("agents"), list):
        raise _AgentsError(502, "mesagents_unavailable", "Réponse de Mes agents illisible.")
    return body


# ─── Texte envoyé à l'agent ─────────────────────────────────────────────────

def neutralize_markers(text: str) -> str:
    """Aère toute suite de 3 ``<`` ou ``>`` et plus : le texte d'une réunion
    ne peut plus fermer le bloc ``<<< … >>>`` ni en ouvrir un autre."""
    text = re.sub(r"<{3,}", lambda m: " ".join(m.group(0)), text)
    return re.sub(r">{3,}", lambda m: " ".join(m.group(0)), text)


def build_user_message(instruction: str, text: str) -> tuple[str, bool]:
    """Consigne puis texte encadré, dans la borne du contrat.

    Renvoie ``(message, tronqué)``. Le texte est coupé — jamais la consigne —
    et la coupe est annoncée à l'agent dans le bloc.
    """
    head = f"{instruction}\n\n{_OPEN}\n"
    tail = f"\n{_CLOSE}"
    body = neutralize_markers(text)
    budget = MAX_MESSAGE_CHARS - len(head) - len(tail)
    truncated = len(body) > budget
    if truncated:
        body = body[: max(0, budget - len(TRUNCATION_NOTE))].rstrip() + TRUNCATION_NOTE
    return head + body + tail, truncated


def _source_text(audio: dict | None, kind: str) -> str:
    if not audio:
        return ""
    raw = audio.get(RUN_KINDS[kind])
    if not raw:
        return ""
    if kind == "meeting_analysis":
        from app.transcript_formats import meeting_analysis_to_markdown
        return meeting_analysis_to_markdown(raw) or ""
    return str(raw)


# ─── Paramètres de la route run ─────────────────────────────────────────────

def _parse_run(agent_id: str) -> tuple[str, str, str | None]:
    if not agent_id or len(agent_id) > MAX_AGENT_ID_CHARS or not _AGENT_ID_RE.match(agent_id):
        raise _AgentsError(400, "invalid_query", "Identifiant d'agent invalide.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise _AgentsError(400, "invalid_query", "Corps JSON attendu.")
    kind = payload.get("kind")
    if kind not in RUN_KINDS:
        raise _AgentsError(400, "invalid_query",
                           "kind doit valoir meeting_analysis, cleaned ou reformulated.")
    instruction = payload.get("instruction")
    if instruction is None:
        instruction = ""
    if not isinstance(instruction, str):
        raise _AgentsError(400, "invalid_query", "instruction doit être un texte.")
    instruction = instruction.strip()
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        raise _AgentsError(400, "invalid_query", "La consigne dépasse 2000 caractères.")
    return kind, instruction or DEFAULT_INSTRUCTIONS[kind], (instruction or None)


# ─── Réunion de la personne ─────────────────────────────────────────────────

def _owned_meeting(user_sub: str, meeting_id: str) -> dict:
    """Même règle que ``GET /api/meetings/<id>`` : device-token-authority ne
    rend que les réunions vivantes de ``user_sub`` — une réunion d'un autre
    compte est un 404, sans distinction."""
    try:
        data = meeting_service.get_meeting(user_sub, meeting_id)
    except req.HTTPError as err:
        status = err.response.status_code if err.response is not None else 502
        if status == 404:
            raise _AgentsError(404, "meeting_not_found", "Réunion introuvable.")
        logger.warning("agents: lecture de la réunion impossible (HTTP %d)", status)
        raise _AgentsError(502, "meeting_unavailable", "La réunion est indisponible.")
    except req.RequestException as exc:
        logger.warning("agents: device-token-authority injoignable (%s)", type(exc).__name__)
        raise _AgentsError(502, "meeting_unavailable", "La réunion est indisponible.")
    meeting = (data or {}).get("meeting")
    if not isinstance(meeting, dict) or not meeting.get("id"):
        raise _AgentsError(404, "meeting_not_found", "Réunion introuvable.")
    return meeting


def _remember_run(user_sub: str, meeting_id: str, entry: dict) -> bool:
    """Écrit la dernière exécution par agent dans ``content["agents"]``.

    ``/amend`` remplace ``content`` en entier : on relit la réunion juste
    avant d'écrire pour ne pas écraser une modification faite pendant
    l'exécution (jusqu'à 120 s). Au mieux : un échec n'enlève pas le résultat
    à la personne, il est seulement signalé (``saved: false``).
    """
    try:
        fresh = _owned_meeting(user_sub, meeting_id)
        content = fresh.get("content")
        content = dict(content) if isinstance(content, dict) else {}
        runs = [r for r in (content.get("agents") or [])
                if isinstance(r, dict) and r.get("id") != entry["id"]]
        content["agents"] = [entry] + runs[: MAX_STORED_RUNS - 1]
        meeting_service.amend_meeting(user_sub, meeting_id, content)
        return True
    except Exception as exc:
        logger.warning("agents: mémorisation impossible sur la réunion (%s)", type(exc).__name__)
        return False


# ─── Routes ─────────────────────────────────────────────────────────────────

@bp.route("/api/agents", methods=["GET"])
@require_auth
def list_agents():
    t0 = time.monotonic()
    try:
        _require_enabled()
        input_kind = (request.args.get("input") or DEFAULT_INPUT).strip()
        if input_kind not in CONTRACT_INPUTS:
            raise _AgentsError(400, "invalid_query", "input hors du vocabulaire du contrat.")
        body = _list_agents(input_kind)
    except _AgentsError as exc:
        return _error(exc.status, exc.code, exc.message, exc.headers)
    user = get_current_user() or {}
    logger.info("agents: liste user=%s input=%s n=%d ms=%d",
                str(user.get("sub") or "")[:12], input_kind, len(body["agents"]),
                round((time.monotonic() - t0) * 1000))
    return jsonify(body)


@bp.route("/api/meetings/<meeting_id>/agents/<agent_id>/run", methods=["POST"])
@require_auth
def run_agent(meeting_id: str, agent_id: str):
    t0 = time.monotonic()
    user = get_current_user() or {}
    user_sub = str(user.get("sub") or "").strip()
    try:
        _require_enabled()
        kind, instruction, custom_instruction = _parse_run(agent_id)
        meeting = _owned_meeting(user_sub, meeting_id)

        # La fiche de l'agent vient de Mes agents, pas du navigateur : nom
        # affiché, et ``model`` à passer (égal à ``id`` aujourd'hui, le contrat
        # dit de ne pas le supposer).
        agents = _list_agents(DEFAULT_INPUT)["agents"]
        agent = next((a for a in agents if isinstance(a, dict) and a.get("id") == agent_id), None)
        if agent is None:
            raise _AgentsError(404, "agent_not_found", "Cet agent n'est pas (ou plus) accessible.")
        agent_name = str(agent.get("name") or agent_id)[:200]
        model = str(agent.get("model") or agent_id)

        uaf_id = meeting.get("user_audio_file_id")
        audio = sessions_service.lookup_audio_outputs_by_uaf_id(user_sub, uaf_id) if uaf_id else None
        text = _source_text(audio, kind).strip()
        if not text:
            raise _AgentsError(409, "text_unavailable",
                               "Ce texte n'existe pas encore pour cette réunion.")
        message, truncated = build_user_message(instruction, text)

        resp = _call("POST", "/v1/chat/completions", json_body={
            "model": model,
            "messages": [{"role": "user", "content": message}],
            "stream": False,
        })
        _raise_common(resp, "l'exécution")
        code, upstream_message = _upstream_error(resp)
        if resp.status_code == 404:
            raise _AgentsError(404, "agent_not_found", "Cet agent n'est pas (ou plus) accessible.")
        if resp.status_code == 422:
            raise _AgentsError(422, code or "blocked", upstream_message or "Demande refusée par l'agent.")
        if resp.status_code != 200:
            logger.warning("agents: exécution HTTP %d code=%s", resp.status_code, code or "-")
            raise _AgentsError(502, "mesagents_unavailable", "L'agent ne répond pas.")
        choices = _json(resp).get("choices")
        output = ""
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            if isinstance(msg, dict):
                output = str(msg.get("content") or "")
        if not output.strip():
            raise _AgentsError(502, "mesagents_unavailable", "L'agent n'a rien répondu.")
    except _AgentsError as exc:
        return _error(exc.status, exc.code, exc.message, exc.headers)

    ran_at = datetime.now(timezone.utc).isoformat()
    entry = {
        "id": agent_id,
        "name": agent_name,
        "kind": kind,
        "instruction": custom_instruction,
        "output": output[:MAX_STORED_OUTPUT_CHARS],
        "input_truncated": truncated,
        "ran_at": ran_at,
    }
    saved = _remember_run(user_sub, meeting_id, entry)
    logger.info("agents: run user=%s meeting=%s agent=%s kind=%s in_chars=%d truncated=%s "
                "out_chars=%d saved=%s ms=%d",
                user_sub[:12], meeting_id, agent_id, kind, len(message), truncated,
                len(output), saved, round((time.monotonic() - t0) * 1000))
    return jsonify({
        "agent": {"id": agent_id, "name": agent_name},
        "kind": kind,
        "output": output,
        "ran_at": ran_at,
        "input_truncated": truncated,
        "saved": saved,
    })
