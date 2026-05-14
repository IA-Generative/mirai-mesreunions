"""
Pre-meeting brief — orchestration code for the ``/meeting-prep`` wizard.

The flow, run synchronously inside the request handler, is::

    user input (subject + role + expectation + focus + duration + drive_folder)
            │
            │  fetch_ciphertext + decrypt → refresh_token
            │  DriveClient.exchange_refresh                  → access_token
            │  DriveClient.list_children(folder_id)          → list[item]
            │  DriveClient.download_item(item_id) (capped)   → bytes
            │  doc_extractor.extract_text                    → str per doc
            ▼
       PREP_DOCS corpus (concatenated, capped)
            │
            │  build_prompt(template, **fields)              → prompt str
            │  LLMClient.chat_json(LLM_MODEL_MEDIUM, ...)    → dict (the brief)
            ▼
       JSON brief returned to the browser

Drive client / doc extractor / LLM client live in ``services/file-mover/app/``
because they are first-class components of the post-meeting pipeline; the
production image co-locates every service's code under ``/app/services`` so
we can load them by absolute path here without duplicating the modules.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Cross-service module loading ─────────────────────────────────

# The runtime image (deploy/docker/Dockerfile) copies *all* services into
# /app/services, and tests resolve the repo root the same way. So a
# path-based import is portable across both contexts.
_FILE_MOVER_APP = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "file-mover", "app")
)


def _load_module(alias: str, basename: str):
    path = os.path.join(_FILE_MOVER_APP, basename)
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"meeting_prep: cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drive_client_mod = _load_module("meeting_prep_drive_client", "drive_client.py")
doc_extractor_mod = _load_module("meeting_prep_doc_extractor", "doc_extractor.py")
llm_client_mod = _load_module("meeting_prep_llm_client", "llm_client.py")

DriveClient = drive_client_mod.DriveClient
DriveAuthError = drive_client_mod.DriveAuthError
DriveTransientError = drive_client_mod.DriveTransientError
DriveApplicativeError = drive_client_mod.DriveApplicativeError
extract_text = doc_extractor_mod.extract_text
LLMClient = llm_client_mod.LLMClient
LLMAuthError = llm_client_mod.LLMAuthError
LLMTransientError = llm_client_mod.LLMTransientError
LLMApplicativeError = llm_client_mod.LLMApplicativeError


# ─── Prompt template path ─────────────────────────────────────────

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
PROMPT_FILES_BY_TYPE = {
    "general": "conductor_brief.txt",
    "one_on_one": "one_on_one.txt",
    "project_update": "project_update.txt",
    "steering_committee": "steering_committee.txt",
    "brainstorm": "brainstorm.txt",
}
DEFAULT_MEETING_TYPE = "general"

# Backwards-compat default — points to the historical "general" prompt so
# callers that don't pass a meeting_type get the same behaviour as before.
PROMPT_PATH = os.path.join(PROMPTS_DIR, PROMPT_FILES_BY_TYPE[DEFAULT_MEETING_TYPE])


def prompt_path_for_type(meeting_type: Optional[str]) -> str:
    """Resolve a meeting_type slug to the absolute path of its prompt file.

    Unknown / empty values fall back to the general prompt (no exception);
    the route validates the whitelist explicitly so this is just a safety
    net for any internal caller.
    """
    key = (meeting_type or "").strip().lower() or DEFAULT_MEETING_TYPE
    filename = PROMPT_FILES_BY_TYPE.get(key, PROMPT_FILES_BY_TYPE[DEFAULT_MEETING_TYPE])
    return os.path.join(PROMPTS_DIR, filename)


# ─── Drive folder ID parsing ──────────────────────────────────────

# The user pastes either a raw item id ("abc-123") or the URL they see in the
# Drive web UI (".../explorer/items/<id>/..." or ".../folders/<id>"). Both
# variants resolve to the same backend item id.
_ITEMS_IN_URL = re.compile(r"/(?:items|folders)/([^/?#\s]+)")


def extract_folder_id(value: str) -> Optional[str]:
    """Return the bare Drive item id from a raw id or from a Drive URL.

    None if the input is empty or contains only whitespace. The function does
    not validate the id format — the Drive backend is the source of truth
    and will return 404 on a bad id.
    """
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    match = _ITEMS_IN_URL.search(stripped)
    if match:
        return match.group(1)
    if "/" in stripped or " " in stripped:
        return None
    return stripped


# ─── Corpus assembly ──────────────────────────────────────────────

# Hard caps to keep the LLM context bounded regardless of folder size.
DEFAULT_MAX_DOCS = 8
DEFAULT_PER_DOC_MAX_CHARS = 20_000
DEFAULT_TOTAL_MAX_CHARS = 80_000
DEFAULT_PER_DOC_MAX_BYTES = 5 * 1024 * 1024


def _item_name(item: dict) -> str:
    """Best-effort display name for a Drive item (used in the corpus header)."""
    for key in ("title", "name", "filename"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return item.get("id", "(sans nom)")


def _is_folder(item: dict) -> bool:
    """Filter folders out of a children listing — we only ingest leaf docs."""
    kind = (item.get("type") or item.get("kind") or "").lower()
    if kind in {"folder", "directory"}:
        return True
    return bool(item.get("is_folder")) or bool(item.get("children"))


def assemble_corpus(
    drive: "DriveClient",
    access_token: str,
    folder_id: str,
    *,
    max_docs: int = DEFAULT_MAX_DOCS,
    per_doc_max_chars: int = DEFAULT_PER_DOC_MAX_CHARS,
    total_max_chars: int = DEFAULT_TOTAL_MAX_CHARS,
    per_doc_max_bytes: int = DEFAULT_PER_DOC_MAX_BYTES,
) -> tuple[str, list[dict]]:
    """List the folder, download + extract each leaf doc, return a concatenated corpus.

    Returns ``(corpus_text, used)`` where ``used`` is a list of metadata dicts
    describing what made it into the prompt (and what was skipped, with the
    reason) — surfaced to the UI so the user sees which docs were ingested.

    Drive errors propagate; the caller maps them to HTTP status codes.
    Per-document extraction errors are captured as ``status="error_extract"``
    so a single corrupt PDF does not kill the whole brief.
    """
    children = drive.list_children(access_token, folder_id)

    used: list[dict] = []
    parts: list[str] = []
    total_chars = 0
    ingested = 0

    for item in children:
        item_id = item.get("id") or ""
        name = _item_name(item)
        if _is_folder(item):
            used.append({"name": name, "id": item_id, "status": "skipped_folder"})
            continue
        if not item_id:
            used.append({"name": name, "id": "", "status": "skipped_no_id"})
            continue
        if ingested >= max_docs:
            used.append({"name": name, "id": item_id, "status": "skipped_doc_cap"})
            continue

        try:
            body, content_type = drive.download_item(
                access_token, item_id, max_bytes=per_doc_max_bytes
            )
        except DriveApplicativeError as exc:
            logger.info("meeting_prep: drive applicative error on %s: %s", item_id, exc)
            used.append({"name": name, "id": item_id, "status": "error_download"})
            continue
        except DriveTransientError:
            # Surface transient errors so the route can return 502 — partial
            # corpus would mislead the LLM. Re-raise.
            raise

        text = extract_text(body, content_type, filename_hint=name, max_chars=per_doc_max_chars)
        if not text:
            used.append({"name": name, "id": item_id, "status": "skipped_unsupported_or_empty"})
            continue

        remaining = max(0, total_max_chars - total_chars)
        if remaining <= 0:
            used.append({"name": name, "id": item_id, "status": "skipped_total_cap"})
            continue
        if len(text) > remaining:
            text = text[:remaining].rstrip() + "\n[…tronqué]"

        parts.append(f"--- {name} ---\n{text}")
        total_chars += len(text)
        ingested += 1
        used.append({
            "name": name,
            "id": item_id,
            "status": "ingested",
            "chars": len(text),
        })

    corpus = "\n\n".join(parts).strip()
    return corpus, used


# ─── Prompt build ─────────────────────────────────────────────────

# Placeholders the template declares. Listed here so a typo in either the
# template or the caller surfaces immediately (KeyError → 500 with a clear
# trace) instead of silently producing a half-substituted prompt.
_REQUIRED_PLACEHOLDERS = (
    "{OBJECTIVE}",
    "{DURATION_MINUTES}",
    "{ROLE_VIEWPOINT}",
    "{EXPECTATION}",
    "{FOCUS_AREAS}",
    "{PREP_DOCS}",
    "{PRIOR_MEETINGS}",
)


def load_prompt_template(path: str = PROMPT_PATH) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    missing = [p for p in _REQUIRED_PLACEHOLDERS if p not in text]
    if missing:
        raise RuntimeError(
            f"meeting_prep: conductor_brief template is missing placeholders: {missing}"
        )
    return text


def build_prompt(
    template_text: str,
    *,
    objective: str,
    duration_minutes: int,
    role_viewpoint: str,
    expectation: str,
    focus_areas: list[str],
    prep_docs_text: str,
    prior_meetings_text: str = "",
) -> str:
    """Substitute the wizard fields into the prompt template.

    ``focus_areas`` is rendered as a comma-separated list; an empty list
    becomes the literal string "(aucun)" so the LLM does not interpret an
    empty placeholder as "all axes".
    """
    focus_repr = ", ".join(a.strip() for a in focus_areas if a and a.strip()) or "(aucun)"
    return (
        template_text
        .replace("{OBJECTIVE}", objective.strip() or "(non précisé)")
        .replace("{DURATION_MINUTES}", str(int(duration_minutes)))
        .replace("{ROLE_VIEWPOINT}", role_viewpoint.strip() or "(non précisé)")
        .replace("{EXPECTATION}", expectation.strip() or "(non précisée)")
        .replace("{FOCUS_AREAS}", focus_repr)
        .replace("{PREP_DOCS}", prep_docs_text.strip() or "(aucun document fourni)")
        .replace("{PRIOR_MEETINGS}", prior_meetings_text.strip() or "(aucun)")
    )
