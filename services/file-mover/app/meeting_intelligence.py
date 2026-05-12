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


# Seuil au-delà duquel on chunke le transcript pour les LLM rewriters
# (glossary / oob_cleaning / reformulation). Calibré pour rester en-dessous
# du timeout HTTP par chunk même sur les modèles medium (mistral-small-24b
# ~ 20s sur 25k chars). Override possible via env :
#   LLM_REWRITE_CHUNK_THRESHOLD : seuil au-dessus duquel on découpe (chars)
#   LLM_REWRITE_CHUNK_SIZE       : taille cible d'un chunk (chars)
#   LLM_REWRITE_CHUNK_OVERLAP    : overlap entre 2 chunks consécutifs (chars)
_CHUNK_THRESHOLD = int(os.getenv("LLM_REWRITE_CHUNK_THRESHOLD", "50000"))
_CHUNK_SIZE      = int(os.getenv("LLM_REWRITE_CHUNK_SIZE",      "25000"))
_CHUNK_OVERLAP   = int(os.getenv("LLM_REWRITE_CHUNK_OVERLAP",   "500"))


def _chunk_text(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Découpe le texte en chunks de ~size chars avec overlap.

    Cherche un séparateur naturel (paragraphe, fin de phrase) près de la
    borne pour éviter de couper en plein milieu d'un mot/phrase. Fallback
    sur hard-split si rien trouvé dans une fenêtre de 2000 chars.
    """
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        end = min(pos + size, len(text))
        if end < len(text):
            window_start = max(pos + size - 2000, pos + 1)
            best = -1
            for sep in ("\n\n", ". ", "? ", "! ", "\n"):
                idx = text.rfind(sep, window_start, end)
                if idx > best:
                    best = idx + len(sep)
            if best > pos:
                end = best
        chunks.append(text[pos:end])
        if end >= len(text):
            break
        pos = max(end - overlap, pos + 1)  # garantit la progression
    return chunks


def _run_llm_rewrite(
    transcript: str,
    llm: LLMClient,
    model: str,
    prompt_template: str,
    *,
    step_name: str,
    extra_subs: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Exécute un LLM-rewrite (glossary / oob_cleaning / reformulation)
    avec chunking automatique si le transcript dépasse le seuil.

    Pour chaque chunk : appelle llm.chat ; en cas d'échec, garde le texte
    brut du chunk plutôt que de tout perdre. Renvoie la concaténation des
    sorties ; None si le transcript est vide.
    """
    if not transcript.strip():
        return None
    extra_subs = extra_subs or {}
    if len(transcript) <= _CHUNK_THRESHOLD:
        prompt = prompt_template.replace("{TRANSCRIPT}", transcript)
        for k, v in extra_subs.items():
            prompt = prompt.replace(k, v)
        try:
            return llm.chat(model, [{"role": "user", "content": prompt}])
        except LLMError:
            logger.warning("%s: LLM call failed", step_name, exc_info=True)
            return None

    chunks = _chunk_text(transcript)
    logger.info(
        "%s: transcript %d chars → %d chunks (size~%d, overlap %d)",
        step_name, len(transcript), len(chunks), _CHUNK_SIZE, _CHUNK_OVERLAP,
    )
    out_parts: list[str] = []
    any_success = False
    for i, ch in enumerate(chunks, 1):
        prompt = prompt_template.replace("{TRANSCRIPT}", ch)
        for k, v in extra_subs.items():
            prompt = prompt.replace(k, v)
        try:
            out = llm.chat(model, [{"role": "user", "content": prompt}])
            out_parts.append(out or ch)
            any_success = True
        except LLMError:
            logger.warning(
                "%s: chunk %d/%d failed, keeping raw text for this section",
                step_name, i, len(chunks), exc_info=True,
            )
            out_parts.append(ch)
    if not any_success:
        return None
    return "\n\n".join(out_parts)


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


def apply_glossary_correction(
    transcript: str,
    llm: LLMClient,
    model: str,
    glossary_terms: list[str],
    max_terms_per_call: int = 200,
) -> Optional[str]:
    """
    Ask the LLM to fix acronyms / specialised terms in the transcript using a
    static glossary. Whisper often spells out unknown sigles phonetically
    ("deux M L F D I" instead of "2MLFDI") — this step rewrites those.

    The glossary is filtered by relevance (see `glossary_loader.filter_relevant`)
    before being embedded in the prompt so we don't burn context with
    hundreds of unrelated entries.

    Returns the corrected transcript, or None when the LLM call failed
    or there was nothing to correct (caller keeps the original).
    """
    if not transcript.strip() or not glossary_terms:
        return None
    # Deferred import so unit tests can stub out the loader independently.
    from app.glossary_loader import filter_relevant

    relevant = filter_relevant(glossary_terms, transcript, max_terms_per_call)
    if not relevant:
        logger.info("glossary_correction: no relevant terms detected, skipping LLM call")
        return None

    logger.info("glossary_correction: %d relevant terms passed to LLM", len(relevant))
    return _run_llm_rewrite(
        transcript, llm, model, _load_prompt("glossary_correction"),
        step_name="glossary_correction",
        extra_subs={"{GLOSSARY_TERMS}": "\n".join(f"- {t}" for t in relevant)},
    )


def clean_oob(transcript: str, llm: LLMClient, model: str) -> Optional[str]:
    """Retire les contenus hors-bande. Chunké automatiquement sur les
    transcriptions longues (cf _run_llm_rewrite)."""
    return _run_llm_rewrite(
        transcript, llm, model, _load_prompt("oob_cleaning"),
        step_name="oob_cleaning",
    )


def reformulate(transcript: str, llm: LLMClient, model: str) -> Optional[str]:
    """Reformule en discours indirect. Chunké automatiquement sur les
    transcriptions longues (cf _run_llm_rewrite)."""
    return _run_llm_rewrite(
        transcript, llm, model, _load_prompt("reformulation"),
        step_name="reformulation",
    )


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


_FORBIDDEN_FILENAME_CHARS_RE = __import__("re").compile(r'[\\/:<>|?*"]')


def suggest_metadata(transcript: str, llm: LLMClient, model: str) -> Optional[dict]:
    """Ask a small LLM to produce a short title + 3-5 key points in one JSON call.

    Returns ``{"title": "...", "key_points": ["...", ...]}`` on success, or
    None on any failure / unparseable response. Best-effort like the other
    steps. Single chat_json call against the small model — cheapest LLM step
    in the kevent pipeline.
    """
    if not transcript.strip():
        return None
    prompt = _render(_load_prompt("suggest_metadata"), transcript)
    try:
        out = llm.chat_json(model, [{"role": "user", "content": prompt}])
    except LLMError:
        logger.warning("suggest_metadata: LLM call failed", exc_info=True)
        return None
    if not isinstance(out, dict):
        logger.warning("suggest_metadata: LLM returned %r, expected dict", type(out))
        return None
    title = (out.get("title") or "").strip()
    # Sanitize : strip forbidden chars + length cap. Empty → "Compte-rendu"
    title = _FORBIDDEN_FILENAME_CHARS_RE.sub(" ", title)
    title = " ".join(title.split())[:80]  # collapse whitespace + cap to 80 chars
    if not title:
        title = "Compte-rendu"
    key_points = out.get("key_points") or []
    if not isinstance(key_points, list):
        key_points = []
    cleaned_points = [
        str(kp).strip() for kp in key_points
        if isinstance(kp, str) and kp.strip()
    ][:5]  # cap at 5 points to control downstream rendering
    return {"title": title, "key_points": cleaned_points}


def serialize_key_points(key_points: list[str] | None) -> Optional[str]:
    """Render the key_points list as a Markdown bullet list for DB storage.

    Returns None for empty/None input so the column stays NULL.
    """
    if not key_points:
        return None
    return "\n".join(f"- {p}" for p in key_points)


def serialize_analysis(analysis: Optional[dict]) -> Optional[str]:
    """Convert the analysis dict to a JSON string suitable for DB storage."""
    if analysis is None:
        return None
    try:
        return json.dumps(analysis, ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("meeting_analysis: dict has non-JSON-serializable values, dropping")
        return None
