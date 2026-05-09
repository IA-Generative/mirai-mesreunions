"""
Orchestration of the four LLM-driven post-transcription steps used by the
``kevent`` backend. Each step is independent and best-effort: a failure
just leaves the corresponding output column NULL — the raw transcription
still ships.

The four steps:

  1. ``extract_speaker_names(text, llm, model_small)`` — small model parses
     introductions in the dialogue ("Bonjour je suis Jean") and returns a
     mapping ``{SPEAKER_NN: real_name}``. Used by the merger to substitute
     anonymous speaker tags with real names before the next steps.

  2. ``clean_oob(text, llm, model_medium)`` — medium model removes
     out-of-band content (parasitic noises transcribed as words, false
     starts, repeated greetings) without changing the meaning.

  3. ``reformulate(text, llm, model_medium)`` — medium model turns the
     verbatim into indirect-speech narrative ("Jean a dit que…, Marie
     a répondu que…").

  4. ``analyse_meeting(text, llm, model_large)`` — large model produces
     the 5-section structured JSON: actors / themes / decisions /
     gaps / recommendations.

Prompts live in the sibling ``prompts/`` directory so they can be edited
without touching the orchestration logic.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

from app.llm_client import LLMClient, LLMError

logger = logging.getLogger(__name__)


_PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def _load_prompt(name: str) -> str:
    """Load a prompt template from prompts/{name}.txt. Caller substitutes {TRANSCRIPT}."""
    path = os.path.join(_PROMPTS_DIR, f"{name}.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _render(template: str, transcript: str) -> str:
    return template.replace("{TRANSCRIPT}", transcript)


def extract_speaker_names(transcript: str, llm: LLMClient, model: str) -> Dict[str, str]:
    """
    Ask the LLM to map ``SPEAKER_NN`` labels to real names found in the
    transcript. Returns the mapping (possibly empty when the LLM couldn't
    detect any name with confidence). Never raises — on failure returns
    ``{}`` and the caller falls back to keeping anonymous labels.
    """
    if not transcript.strip():
        return {}
    prompt = _render(_load_prompt("speaker_names"), transcript)
    messages = [{"role": "user", "content": prompt}]
    try:
        mapping = llm.chat_json(model, messages)
    except LLMError:
        logger.warning("speaker_names: LLM call failed, keeping anonymous labels", exc_info=True)
        return {}
    if not isinstance(mapping, dict):
        logger.warning("speaker_names: LLM did not return an object, got %r", type(mapping))
        return {}
    # Filter out garbage entries: keys must start with SPEAKER_, values must be non-empty strings.
    cleaned = {}
    for k, v in mapping.items():
        if not isinstance(k, str) or not k.startswith("SPEAKER_"):
            continue
        if not isinstance(v, str) or not v.strip():
            continue
        if v.strip() == k:
            # Model couldn't determine the name — keep the anonymous tag.
            continue
        cleaned[k] = v.strip()
    logger.info("speaker_names: %d/%d labels resolved", len(cleaned), len(mapping))
    return cleaned


def clean_oob(transcript: str, llm: LLMClient, model: str) -> Optional[str]:
    """
    Ask the LLM to remove out-of-band content. Returns the cleaned text
    or None if the call failed (caller falls back to original transcript).
    """
    if not transcript.strip():
        return None
    prompt = _render(_load_prompt("oob_cleaning"), transcript)
    messages = [{"role": "user", "content": prompt}]
    try:
        return llm.chat(model, messages)
    except LLMError:
        logger.warning("oob_cleaning: LLM call failed", exc_info=True)
        return None


def reformulate(transcript: str, llm: LLMClient, model: str) -> Optional[str]:
    """
    Ask the LLM to turn verbatim into indirect-speech narrative.
    Returns the reformulated text or None on failure.
    """
    if not transcript.strip():
        return None
    prompt = _render(_load_prompt("reformulation"), transcript)
    messages = [{"role": "user", "content": prompt}]
    try:
        return llm.chat(model, messages)
    except LLMError:
        logger.warning("reformulation: LLM call failed", exc_info=True)
        return None


def analyse_meeting(transcript: str, llm: LLMClient, model: str) -> Optional[dict]:
    """
    Ask the (large) LLM for the structured 5-section meeting analysis.
    Returns the parsed dict, or None on failure (so the caller leaves the
    column NULL rather than store invalid JSON).
    """
    if not transcript.strip():
        return None
    prompt = _render(_load_prompt("meeting_analysis"), transcript)
    messages = [{"role": "user", "content": prompt}]
    try:
        return llm.chat_json(model, messages)
    except LLMError:
        logger.warning("meeting_analysis: LLM call failed", exc_info=True)
        return None


def serialize_analysis(analysis: Optional[dict]) -> Optional[str]:
    """Convert the analysis dict to a JSON string suitable for DB storage."""
    if analysis is None:
        return None
    try:
        return json.dumps(analysis, ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("meeting_analysis: dict has non-JSON-serializable values, dropping")
        return None
