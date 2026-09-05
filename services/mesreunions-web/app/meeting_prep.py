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

Drive client / doc extractor / LLM client live in ``services/dmz-to-internal-bridge/app/``
because they are first-class components of the post-meeting pipeline; the
production image co-locates every service's code under ``/app/services`` so
we can load them by absolute path here without duplicating the modules.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import unicodedata
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

# ─── Cross-service module loading ─────────────────────────────────

# The runtime image (deploy/docker/Dockerfile) copies *all* services into
# /app/services, and tests resolve the repo root the same way. So a
# path-based import is portable across both contexts.
_FILE_MOVER_APP = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "dmz-to-internal-bridge", "app")
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
    """Return the bare Drive item id from a raw id or from a Drive URL."""
    fid, _ = extract_folder_id_and_host(value)
    return fid


def extract_folder_id_and_host(value: str) -> tuple[Optional[str], Optional[str]]:
    """Parse l'input user et retourne (folder_id, hostname).

    Cas :
      - URL Drive ``https://X/.../items/<id>/...`` ou ``.../folders/<id>``
        → (id, X)
      - URL Drive sans match items/folders → (None, X) ; le caller refusera
      - ID nu (sans / ni espace) → (id, None) ; le caller utilise le base URL
        par défaut (DRIVE_BASE_URL env)
      - vide / blanc → (None, None)
    """
    if not value:
        return None, None
    stripped = value.strip()
    if not stripped:
        return None, None
    host: Optional[str] = None
    # Hostname si URL : on parse avant la recherche de l'id pour gérer aussi
    # le cas "URL sans /items/" (Drive root, partage de dossier explorer/...).
    if stripped.startswith("http://") or stripped.startswith("https://"):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(stripped)
            host = (parsed.hostname or None)
        except Exception:
            host = None
    match = _ITEMS_IN_URL.search(stripped)
    if match:
        return match.group(1), host
    if "/" in stripped or " " in stripped:
        return None, host
    return stripped, host


# ─── Corpus assembly ──────────────────────────────────────────────

# Hard caps to keep the LLM context bounded regardless of folder size.
DEFAULT_MAX_DOCS = 8
DEFAULT_PER_DOC_MAX_CHARS = 20_000
DEFAULT_TOTAL_MAX_CHARS = 80_000
DEFAULT_PER_DOC_MAX_BYTES = 5 * 1024 * 1024

# Budgets par emplacement du prompt. Ils sont séparés parce que {PREP_DOCS},
# {PRIOR_MEETINGS} et {INLINE_MESSAGES} sont trois sections distinctes : un
# budget commun laisserait un gros dossier Drive évincer silencieusement les
# réunions passées, que l'utilisateur a pourtant choisies explicitement.
DEFAULT_BUDGETS = {
    "prep_docs": DEFAULT_TOTAL_MAX_CHARS,
    "prior_meetings": 30_000,
    "inline_messages": 20_000,
}


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


# ─── Assainissement des contenus de source ────────────────────────
#
# Tout texte entrant dans le corpus est une DONNÉE, pas une consigne. Deux
# familles de contenus ne sont pas de confiance : les messages collés par
# l'utilisateur (origine ``inline``) et — moins évident mais tout aussi vrai —
# les documents Drive, dont le NOM est choisi par qui a partagé le dossier.

# Caractères invisibles : zéro-largeur et overrides bidirectionnels. Ce sont
# les vecteurs classiques d'instructions cachées dans un texte d'apparence
# anodine (le lecteur humain ne voit rien, le modèle lit tout).
_INVISIBLE_RE = re.compile(r"[​-‏⁠-⁯﻿‪-‮]")
# Caractères de contrôle, sauf tabulation et saut de ligne.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MANY_NEWLINES_RE = re.compile(r"\n{4,}")


def sanitize_source_text(text: str) -> str:
    """Normalise un texte de source avant de le verser au corpus.

    Ne fait pas d'anti-XSS (rien n'est rendu en HTML) : retire ce qui permet
    de dissimuler des instructions à un relecteur humain tout en restant
    lisible par le modèle.
    """
    if not isinstance(text, str) or not text:
        return ""
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = _INVISIBLE_RE.sub("", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _MANY_NEWLINES_RE.sub("\n\n\n", cleaned)
    return cleaned.strip()


def new_corpus_nonce() -> str:
    """Jeton imprévisible identifiant les frontières de document d'UNE génération."""
    return uuid.uuid4().hex[:8]


def format_source_header(nonce: str, name: str, origin: str) -> str:
    """En-tête de document infalsifiable.

    Le corpus séparait les documents par ``--- {nom} ---``. N'importe quel
    contenu — un mail, ou simplement un fichier Drive nommé
    ``--- Note officielle ---`` — pouvait donc fabriquer une fausse frontière
    et faire passer son texte pour un autre document, voire pour la consigne
    système. Le nonce n'étant pas devinable, la frontière redevient fiable.
    """
    safe_name = (name or "(sans nom)").replace("\n", " ").strip()[:200]
    return f"--- [SRC {nonce}] {safe_name} · {origin} ---"


def _strip_nonce(text: str, nonce: str) -> str:
    """Retire du contenu toute occurrence du nonce (défense en profondeur)."""
    if not nonce or nonce not in text:
        return text
    return text.replace(nonce, "")


# ─── Fournisseurs de sources ──────────────────────────────────────
#
# Un fournisseur expose deux choses :
#   - ``count_hint()`` : nombre d'items candidats, pour le stepper UI ;
#   - ``items()`` : itérateur de ``SourceItem``.
#
# La PARESSE est structurante : ``load(budget)`` n'est appelé que si l'item a
# encore sa place. Un fournisseur qui rendrait une liste déjà chargée
# téléchargerait tout pour en jeter la moitié — c'est exactement ce que le
# code d'origine évitait en s'arrêtant à ``ingested >= max_docs``.


class SourceItem:
    """Un candidat au corpus. ``load(budget) -> (texte|None, statut)``."""

    __slots__ = ("name", "ref", "origin", "load")

    def __init__(self, name: str, ref: str, origin: str, load):
        self.name = name
        self.ref = ref
        self.origin = origin
        self.load = load


class DriveFolderProvider:
    """Les documents feuilles d'un dossier Drive (comportement historique)."""

    bucket = "prep_docs"

    def __init__(self, drive, access_token: str, folder_id: str, *,
                 per_doc_max_bytes: int = DEFAULT_PER_DOC_MAX_BYTES,
                 per_doc_max_chars: int = DEFAULT_PER_DOC_MAX_CHARS):
        self._drive = drive
        self._token = access_token
        self._folder_id = folder_id
        self._per_doc_max_bytes = per_doc_max_bytes
        self._per_doc_max_chars = per_doc_max_chars
        self._children = None

    def _load_children(self) -> list:
        if self._children is None:
            self._children = self._drive.list_children(self._token, self._folder_id) or []
        return self._children

    def count_hint(self) -> int:
        return sum(1 for it in self._load_children() if not _is_folder(it))

    def items(self):
        for item in self._load_children():
            item_id = item.get("id") or ""
            name = _item_name(item)
            if _is_folder(item):
                yield SourceItem(name, item_id, "drive", _rejected("skipped_folder"))
                continue
            if not item_id:
                yield SourceItem(name, "", "drive", _rejected("skipped_no_id"))
                continue
            yield SourceItem(name, item_id, "drive", self._make_loader(item_id, name))

    def _make_loader(self, item_id: str, name: str):
        def _load(_budget: int):
            try:
                body, content_type = self._drive.download_item(
                    self._token, item_id, max_bytes=self._per_doc_max_bytes
                )
            except DriveApplicativeError as exc:
                logger.info("meeting_prep: drive applicative error on %s: %s", item_id, exc)
                return None, "error_download"
            except DriveTransientError as exc:
                # 2026-05-24 : best-effort sur les erreurs transitoires visant
                # UN document — vu en prod des HTTP 500 systématiques sur
                # certains fichiers. On saute le fautif, le brief se construit
                # sur les autres.
                logger.warning(
                    "meeting_prep: drive transient error on %s (skip + continue): %s",
                    item_id, exc,
                )
                return None, "error_transient"
            text = extract_text(
                body, content_type, filename_hint=name, max_chars=self._per_doc_max_chars
            )
            if not text:
                return None, "skipped_unsupported_or_empty"
            return text, "ingested"
        return _load


class DriveFilesProvider:
    """Une sélection explicite de fichiers Drive (pas de listing préalable)."""

    bucket = "prep_docs"

    def __init__(self, drive, access_token: str, items: list, *,
                 per_doc_max_bytes: int = DEFAULT_PER_DOC_MAX_BYTES,
                 per_doc_max_chars: int = DEFAULT_PER_DOC_MAX_CHARS):
        self._drive = drive
        self._token = access_token
        self._items = items or []
        self._per_doc_max_bytes = per_doc_max_bytes
        self._per_doc_max_chars = per_doc_max_chars

    def count_hint(self) -> int:
        return len(self._items)

    def items(self):
        for entry in self._items:
            item_id = (entry.get("id") or "").strip()
            name = (entry.get("name") or "").strip() or item_id or "(sans nom)"
            if not item_id:
                yield SourceItem(name, "", "drive", _rejected("skipped_no_id"))
                continue
            yield SourceItem(name, item_id, "drive", self._make_loader(item_id, name))

    def _make_loader(self, item_id: str, name: str):
        def _load(_budget: int):
            try:
                body, content_type = self._drive.download_item(
                    self._token, item_id, max_bytes=self._per_doc_max_bytes
                )
            except DriveApplicativeError as exc:
                logger.info("meeting_prep: drive applicative error on %s: %s", item_id, exc)
                return None, "error_download"
            except DriveTransientError as exc:
                logger.warning(
                    "meeting_prep: drive transient error on %s (skip + continue): %s",
                    item_id, exc,
                )
                return None, "error_transient"
            text = extract_text(
                body, content_type, filename_hint=name, max_chars=self._per_doc_max_chars
            )
            if not text:
                return None, "skipped_unsupported_or_empty"
            return text, "ingested"
        return _load


class InlineProvider:
    """Textes collés par l'utilisateur (mail, note). Aucune I/O.

    Le contenu est déjà assaini au parsing du payload : ici on ne fait que
    le verser sous plafond.
    """

    bucket = "inline_messages"

    def __init__(self, entries: list):
        self._entries = entries or []

    def count_hint(self) -> int:
        return len(self._entries)

    def items(self):
        for idx, entry in enumerate(self._entries):
            title = entry.get("title") or "Texte collé"
            text = entry.get("text") or ""
            yield SourceItem(title, f"inline:{idx}", "inline", self._make_loader(text))

    def _make_loader(self, text: str):
        def _load(budget: int):
            if not text:
                return None, "skipped_unsupported_or_empty"
            return text[:budget] if budget and len(text) > budget else text, "ingested"
        return _load


class PriorMeetingProvider:
    """Réunions passées : brief, points clés et compte-rendu.

    ``fetch`` est injecté (une fonction ``(preparation_id, include) -> texte``)
    pour que le module reste testable sans réseau et sans connaître le
    transport interne.
    """

    bucket = "prior_meetings"

    def __init__(self, fetch, entries: list):
        self._fetch = fetch
        self._entries = entries or []

    def count_hint(self) -> int:
        return len(self._entries)

    def items(self):
        for entry in self._entries:
            prep_id = entry.get("id") or ""
            label = entry.get("label") or "Réunion précédente"
            yield SourceItem(label, prep_id, "preparation",
                             self._make_loader(prep_id, entry.get("include") or []))

    def _make_loader(self, prep_id: str, include: list):
        def _load(_budget: int):
            try:
                text = self._fetch(prep_id, include)
            except Exception:
                # Une réunion illisible dégrade cette source, jamais la
                # génération entière.
                logger.exception("meeting_prep: prior meeting fetch failed for %s", prep_id)
                return None, "error_internal_api"
            if not text or not text.strip():
                return None, "skipped_empty_source"
            return text, "ingested"
        return _load


def _rejected(status: str):
    """Fabrique un ``load`` qui refuse d'emblée, sans I/O."""
    def _load(_budget: int):
        return None, status
    return _load


def build_corpus(
    providers: list,
    *,
    max_docs: int = DEFAULT_MAX_DOCS,
    per_doc_max_chars: int = DEFAULT_PER_DOC_MAX_CHARS,
    budgets: "dict[str, int] | None" = None,
    progress=None,
    nonce: "str | None" = None,
) -> tuple[dict, list[dict]]:
    """Assemble le corpus à partir de fournisseurs, sous plafonds explicites.

    Retourne ``({bucket: texte}, used)``. Les budgets sont **par bucket** et
    non globaux : ``{PREP_DOCS}`` et ``{PRIOR_MEETINGS}`` occupent deux
    emplacements distincts du prompt, et un budget unique laisserait un gros
    dossier Drive évincer silencieusement les réunions passées.

    ``progress`` reçoit les mêmes phases qu'avant (``listing_docs`` puis
    ``reading_doc``) : ce sont des valeurs persistées en base qui pilotent
    l'animation, on n'en invente pas de nouvelles.
    """
    def _emit(**kw):
        if progress is None:
            return
        try:
            progress(**kw)
        except Exception:  # pragma: no cover — l'UI ne casse jamais le pipeline
            logger.exception("meeting_prep: progress callback raised")

    budgets = dict(budgets or {})
    nonce = nonce or new_corpus_nonce()

    _emit(phase="listing_docs")
    docs_total = 0
    for provider in providers:
        try:
            docs_total += provider.count_hint()
        except Exception:
            logger.exception("meeting_prep: count_hint failed on %s", type(provider).__name__)
    docs_total = min(docs_total, max_docs)
    _emit(phase="listing_docs", docs_total=docs_total)

    used: list[dict] = []
    parts: "dict[str, list[str]]" = {}
    spent: "dict[str, int]" = {}
    ingested = 0

    for provider in providers:
        bucket = getattr(provider, "bucket", "prep_docs")
        budget_total = budgets.get(bucket, DEFAULT_TOTAL_MAX_CHARS)
        for item in provider.items():
            entry = {"name": item.name, "id": item.ref, "origin": item.origin}

            if ingested >= max_docs:
                used.append({**entry, "status": "skipped_doc_cap"})
                continue

            remaining = max(0, budget_total - spent.get(bucket, 0))
            if remaining <= 0:
                used.append({**entry, "status": "skipped_source_cap"})
                continue

            _emit(
                phase="reading_doc",
                current_doc=item.name,
                docs_processed=ingested,
                docs_total=docs_total,
            )

            text, status = item.load(min(remaining, per_doc_max_chars))
            if not text or status != "ingested":
                used.append({**entry, "status": status})
                continue

            text = _strip_nonce(text, nonce)
            truncated = False
            if len(text) > remaining:
                text = text[:remaining].rstrip() + "\n[…tronqué]"
                truncated = True

            parts.setdefault(bucket, []).append(
                f"{format_source_header(nonce, item.name, item.origin)}\n{text}"
            )
            spent[bucket] = spent.get(bucket, 0) + len(text)
            ingested += 1
            record = {**entry, "status": "ingested", "chars": len(text)}
            if truncated:
                record["truncated"] = True
            used.append(record)
            _emit(
                phase="reading_doc",
                current_doc=item.name,
                docs_processed=ingested,
                docs_total=docs_total,
            )

    buckets = {name: "\n\n".join(chunks).strip() for name, chunks in parts.items()}
    return buckets, used


def normalize_drive_item(item: dict) -> dict:
    """Vue stable d'un item Drive pour l'UI.

    L'API ne garantit aucun schéma : selon les instances on observe ``title``
    ou ``name``, ``type`` ou ``kind``. Cette normalisation était écrite en
    double (ici et dans la route de diagnostic), avec des différences subtiles
    sur ce qui compte comme dossier — une seule définition vaut mieux.
    """
    return {
        "id": item.get("id") or "",
        "name": _item_name(item),
        "is_folder": _is_folder(item),
        "mime_type": item.get("mime_type") or item.get("mimetype") or None,
        "size": item.get("size"),
        "updated_at": item.get("updated_at") or item.get("modified_at") or None,
    }


def assemble_corpus(
    drive: "DriveClient",
    access_token: str,
    folder_id: str,
    *,
    max_docs: int = DEFAULT_MAX_DOCS,
    per_doc_max_chars: int = DEFAULT_PER_DOC_MAX_CHARS,
    total_max_chars: int = DEFAULT_TOTAL_MAX_CHARS,
    per_doc_max_bytes: int = DEFAULT_PER_DOC_MAX_BYTES,
    progress=None,
) -> tuple[str, list[dict]]:
    """Corpus d'un unique dossier Drive — signature historique, préservée.

    Conserve strictement le contrat d'origine ``(corpus_text, used)`` : c'est
    le chemin emprunté quand l'utilisateur ne fournit qu'un dossier Drive, et
    les appelants comme les tests s'y adossent. Le travail réel est fait par
    ``build_corpus``.
    """
    buckets, used = build_corpus(
        [DriveFolderProvider(
            drive, access_token, folder_id,
            per_doc_max_bytes=per_doc_max_bytes,
            per_doc_max_chars=per_doc_max_chars,
        )],
        max_docs=max_docs,
        per_doc_max_chars=per_doc_max_chars,
        budgets={"prep_docs": total_max_chars},
        progress=progress,
    )
    return buckets.get("prep_docs", ""), used


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

# Placeholders optionnels. Présents dans les 5 templates mais non listés en
# REQUIRED pour permettre une migration progressive des templates sans casser
# load_prompt_template().
#   - {PRIOR_KEY_POINTS} : key_points_summary du dernier audio lié au brief
#     parent de la série (meeting-prep v2 §6).
#   - {SUCCESS_CRITERIA} : ce que le demandeur espère avoir obtenu à la fin
#     de la réunion (coaching wizard) — nourrit la zone ai_recommendations.
_OPTIONAL_PLACEHOLDERS = (
    "{PRIOR_KEY_POINTS}",
    "{SUCCESS_CRITERIA}",
    "{INLINE_MESSAGES}",
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
    prior_key_points_text: str = "",
    success_criteria_text: str = "",
    inline_messages_text: str = "",
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
        .replace("{PRIOR_KEY_POINTS}", prior_key_points_text.strip() or "(aucun)")
        .replace("{SUCCESS_CRITERIA}", success_criteria_text.strip() or "(non précisé)")
        .replace("{INLINE_MESSAGES}", inline_messages_text.strip() or "(aucun)")
    )
