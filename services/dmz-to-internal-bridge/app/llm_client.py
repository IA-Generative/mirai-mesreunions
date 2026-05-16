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
from typing import List, Optional

import requests as req

logger = logging.getLogger(__name__)


# ─── Exceptions ────────────────────────────────────────────────

class LLMError(Exception):
    """Base class — never raised directly."""


class LLMAuthError(LLMError):
    """401/403: bearer rejected by LiteLLM."""


class LLMTransientError(LLMError):
    """5xx, timeout, conn reset."""


class LLMApplicativeError(LLMError):
    """4xx other: context too long, bad model name, malformed request."""


# ─── Client ────────────────────────────────────────────────────

class LLMClient:
    """
    Minimal OpenAI-compatible wrapper. Synchronous, stateless.

    The chat() helper handles the (small) JSON body construction and the
    error-classification logic. Tool calls and streaming are out of scope —
    we only need plain text or structured JSON output.
    """

    def __init__(self, base_url: str, api_key: str, timeout: int = 180):
        if not base_url:
            raise ValueError("LITELLM_BASE_URL is required to build LLMClient")
        if not api_key:
            raise ValueError("LITELLM_API_KEY is required to build LLMClient")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def _raise_for_status(resp) -> None:
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"LiteLLM auth failed: {resp.status_code}")
        if resp.status_code >= 500:
            raise LLMTransientError(f"LiteLLM 5xx: {resp.status_code}: {(resp.text or '')[:200]}")
        if resp.status_code >= 400:
            raise LLMApplicativeError(f"LiteLLM {resp.status_code}: {(resp.text or '')[:200]}")

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
