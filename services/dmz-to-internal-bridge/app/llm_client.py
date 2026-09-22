"""
Tiny OpenAI-compatible chat client for the LiteLLM Mirai hub.

LiteLLM exposes ``POST /v1/chat/completions`` with the standard OpenAI
schema, behind the standard ``Authorization: Bearer <token>`` header (note
the contrast with Kevent which uses a non-standard ``apikey`` header).

Used by ``meeting_intelligence.py`` for the four post-transcription
LLM steps : speaker naming, OOB cleaning, reformulation, meeting analysis.

Errors are classified the same way as the Kevent client so the orchestration
layer can react uniformly:

  - ``LLMAuthError``        : 401/403. The shared LiteLLM key is wrong or
                              quota-exhausted.
  - ``LLMTransientError``   : 5xx, timeout, network. Caller decides whether
                              to retry (most callers don't — meeting
                              intelligence is best-effort: a failed step
                              just leaves the corresponding column NULL,
                              the raw transcription still ships).
  - ``LLMApplicativeError`` : 4xx other (context too long, bad model name,
                              malformed request).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional

import requests as req

logger = logging.getLogger(__name__)

# Modèles de repli quand le hub refuse le nom demandé (« Invalid model name »).
#
# Le catalogue du hub LiteLLM bouge sans préavis (le 2026-08-25 `gpt-oss-120b`
# est devenu `gptoss-120b` ; le 2026-09-16 `mistral-small-24b` et `chat-small`
# répondaient 400 « Invalid model name passed in model=… » pour notre clé).
# Un nom inconnu ne se voit ni au démarrage ni dans une sonde : il fait
# simplement échouer chaque étape LLM, et le compte-rendu sort vide.
# `chat` est l'alias générique servi à toutes les applications de la bêta,
# `gptoss-120b` le grand modèle utilisé par l'analyse de réunion.
_DEFAULT_FALLBACK_MODELS = "chat,gptoss-120b"

# Mémoire de processus des noms refusés par le hub, clé (base_url, modèle) :
# on ne paie l'aller-retour 400 qu'une fois par processus, pas à chaque étape.
_UNKNOWN_MODELS: set = set()

_UNKNOWN_MODEL_PATTERNS = (
    re.compile(r"invalid model name", re.I),
    re.compile(r"model[_ ]not[_ ]found", re.I),
    re.compile(r"model .{0,80}(does not exist|not exist|unknown|introuvable)", re.I),
)


def fallback_models_from_env() -> List[str]:
    """Liste des modèles de repli lue dans ``LLM_MODEL_FALLBACKS`` (CSV).

    Une valeur vide désactive le repli ; l'absence de variable donne
    ``_DEFAULT_FALLBACK_MODELS``.
    """
    raw = os.environ.get("LLM_MODEL_FALLBACKS")
    if raw is None:
        raw = _DEFAULT_FALLBACK_MODELS
    return [m.strip() for m in raw.split(",") if m.strip()]


def is_unknown_model_response(status_code: int, text: str) -> bool:
    """Vrai si la réponse du hub dit « ce nom de modèle n'existe pas »."""
    if status_code not in (400, 404):
        return False
    body = text or ""
    return any(p.search(body) for p in _UNKNOWN_MODEL_PATTERNS)


# ─── Exceptions ────────────────────────────────────────────────

class LLMError(Exception):
    """Base class — never raised directly."""


class LLMAuthError(LLMError):
    """401/403: bearer rejected by LiteLLM."""


class LLMTransientError(LLMError):
    """5xx, timeout, conn reset."""


class LLMApplicativeError(LLMError):
    """4xx other: context too long, bad model name, malformed request.

    ``status_code`` et ``body`` (facultatifs) permettent au client de
    reconnaître un nom de modèle inconnu et de tenter un repli.
    """

    def __init__(self, message: str, status_code: int = 0, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


# ─── Client ────────────────────────────────────────────────────

class LLMClient:
    """
    Minimal OpenAI-compatible wrapper. Synchronous, stateless.

    The chat() helper handles the (small) JSON body construction and the
    error-classification logic. Tool calls and streaming are out of scope —
    we only need plain text or structured JSON output.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 180,
        fallback_models: Optional[List[str]] = None,
    ):
        if not base_url:
            raise ValueError("LITELLM_BASE_URL is required to build LLMClient")
        if not api_key:
            raise ValueError("LITELLM_API_KEY is required to build LLMClient")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # ``None`` → lecture de l'environnement ; ``[]`` → aucun repli.
        self.fallback_models = (
            list(fallback_models) if fallback_models is not None
            else fallback_models_from_env()
        )

    # ── Repli de modèle ─────────────────────────────────────────

    def _candidates(self, model: str) -> List[str]:
        """Le modèle demandé puis les replis, sans doublon, en gardant
        l'ordre. Les noms déjà refusés par ce hub passent en fin de liste
        (on les retente seulement si tout le reste échoue aussi)."""
        ordered: List[str] = []
        for m in [model, *self.fallback_models]:
            if m and m not in ordered:
                ordered.append(m)
        known_bad = [m for m in ordered if (self.base_url, m) in _UNKNOWN_MODELS]
        return [m for m in ordered if m not in known_bad] + known_bad

    def _remember_unknown(self, model: str) -> None:
        key = (self.base_url, model)
        if key not in _UNKNOWN_MODELS:
            _UNKNOWN_MODELS.add(key)
            logger.warning(
                "LiteLLM refuse le modèle %r (Invalid model name) — repli sur %s ; "
                "corriger LLM_MODEL_SMALL/MEDIUM/LARGE pour ne plus payer ce détour",
                model, self.fallback_models or "aucun repli configuré",
            )

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _raise_for_status(resp) -> None:
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"LiteLLM auth failed: {resp.status_code}")
        if resp.status_code >= 500:
            raise LLMTransientError(f"LiteLLM 5xx: {resp.status_code}: {(resp.text or '')[:200]}")
        if resp.status_code >= 400:
            raise LLMApplicativeError(
                f"LiteLLM {resp.status_code}: {(resp.text or '')[:200]}",
                status_code=resp.status_code, body=resp.text or "",
            )

    def chat(
        self,
        model: str,
        messages: List[dict],
        response_format: Optional[dict] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Single-turn chat completion. Returns the assistant's content as
        a plain string. When ``response_format={"type":"json_object"}``,
        the content is the JSON-encoded structured output (callers parse
        with ``json.loads``).
        """
        first_error: Optional[LLMApplicativeError] = None
        for candidate in self._candidates(model):
            try:
                resp = self._post_completion(
                    candidate, messages, response_format, temperature, max_tokens,
                )
            except LLMApplicativeError as exc:
                if not is_unknown_model_response(exc.status_code, exc.body):
                    raise
                # Nom de modèle inconnu du hub : on passe au repli suivant.
                self._remember_unknown(candidate)
                first_error = first_error or exc
                continue
            if candidate != model:
                logger.info("LiteLLM: %r servi par le modèle de repli %r", model, candidate)
            return self._extract_content(resp)
        assert first_error is not None
        raise first_error

    def _post_completion(
        self,
        model: str,
        messages: List[dict],
        response_format: Optional[dict],
        temperature: float,
        max_tokens: Optional[int],
    ):
        url = f"{self.base_url}/v1/chat/completions"
        body = {"model": model, "messages": messages, "temperature": temperature}
        if response_format is not None:
            body["response_format"] = response_format
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            resp = req.post(
                url,
                json=body,
                headers={**self._auth_header(), "Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except req.RequestException as exc:
            raise LLMTransientError(f"LiteLLM /chat/completions unreachable: {exc}") from exc
        self._raise_for_status(resp)
        return resp

    @staticmethod
    def _extract_content(resp) -> str:
        try:
            choices = resp.json().get("choices", [])
        except Exception as exc:
            raise LLMTransientError(f"LiteLLM response not JSON: {exc}") from exc
        if not choices:
            raise LLMApplicativeError("LiteLLM response has no choices")
        content = (choices[0].get("message", {}) or {}).get("content", "")
        if not isinstance(content, str):
            raise LLMApplicativeError("LiteLLM choice has no string content")
        return content

    def chat_json(self, model: str, messages: List[dict], temperature: float = 0.2) -> dict:
        """
        Chat with ``response_format=json_object``. Parses the result as JSON
        and returns the dict. Raises ``LLMApplicativeError`` if the model
        produced something we can't parse.
        """
        raw = self.chat(model, messages, response_format={"type": "json_object"}, temperature=temperature)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMApplicativeError(f"LiteLLM returned invalid JSON: {exc}; raw={raw[:200]}") from exc
