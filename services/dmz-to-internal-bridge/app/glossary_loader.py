"""Load static glossary files for post-transcription LLM correction.

Supported formats (all UTF-8) :
  - `.md`  : `**TERM** - definition - extra` (one entry per non-empty paragraph)
             extracts only the bold term — definition is dropped (we just need
             the canonical spelling for the LLM).
  - `.txt` : one term per line. Lines starting with `#` are ignored.
  - `.json`: top-level list, each item is either a string or
             `{"term": "...", ...}`.

The loader is best-effort : malformed files log a warning and are skipped.
The output is a deduplicated, sorted list of term strings ready to be embedded
in the LLM correction prompt. Definitions are intentionally NOT shipped to the
LLM — they would burn context and the LLM only needs the canonical spelling
to produce a correction.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


_MD_BOLD_TERM_RE = re.compile(r"\*\*([^*\n]+?)\*\*")


def _parse_md(content: str) -> list[str]:
    """Extract every `**TERM**` occurrence — the part before " - " or end of line."""
    out: list[str] = []
    for match in _MD_BOLD_TERM_RE.finditer(content):
        term = match.group(1).strip()
        if not term:
            continue
        # Some entries have alternates like `**AC** ou **ADCE**` — both extracted
        # by the regex naturally. Strip any trailing punctuation just in case.
        term = term.strip(" .,;:")
        if term:
            out.append(term)
    return out


def _parse_txt(content: str) -> list[str]:
    out: list[str] = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _parse_json(content: str) -> list[str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("expected top-level list")
    out: list[str] = []
    for entry in data:
        if isinstance(entry, str):
            t = entry.strip()
        elif isinstance(entry, dict):
            t = str(entry.get("term") or entry.get("name") or "").strip()
        else:
            continue
        if t:
            out.append(t)
    return out


def load_glossary_dir(directory: str | Path) -> list[str]:
    """Load every supported glossary file under `directory`. Returns deduplicated terms.

    Missing or empty directory → empty list, no exception (best-effort).
    """
    p = Path(directory)
    if not p.is_dir():
        logger.info("Glossary directory %s missing — using empty glossary.", p)
        return []
    seen: set[str] = set()
    terms: list[str] = []
    for child in sorted(p.iterdir()):
        if not child.is_file():
            continue
        suffix = child.suffix.lower()
        try:
            content = child.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("Cannot read glossary file %s: %s", child, e)
            continue
        try:
            if suffix == ".md":
                items = _parse_md(content)
            elif suffix == ".txt":
                items = _parse_txt(content)
            elif suffix == ".json":
                items = _parse_json(content)
            else:
                continue
        except Exception as e:
            logger.warning("Cannot parse glossary file %s: %s", child, e)
            continue
        added = 0
        for t in items:
            if t in seen:
                continue
            seen.add(t)
            terms.append(t)
            added += 1
        logger.info("Glossary %s : %d new terms (total %d)", child.name, added, len(terms))
    return terms


def filter_relevant(terms: list[str], transcript: str, max_terms: int) -> list[str]:
    """Pick at most `max_terms` glossary entries likely relevant to the transcript.

    Heuristic — we keep the terms whose **first letters** appear *as a sequence
    of letter-words* in the transcript (Whisper tends to spell out unknown
    acronyms : "DAGEM" → "dé a gé eu emm" or "D A G E M"). We don't try to
    match definitions ; that's the LLM's job at correction time.

    Always returns terms sorted alphabetically (deterministic for cache hits).
    """
    if not terms or not transcript:
        return []
    norm = transcript.lower()
    norm = re.sub(r"[^\w\s']", " ", norm)
    norm_compact = re.sub(r"\s+", " ", norm)
    words = norm_compact.split()
    word_set = set(words)

    candidates: list[tuple[int, str]] = []
    for term in terms:
        # Letters-only canonical form for matching
        letters = re.sub(r"[^A-Za-zÀ-ÖØ-öø-ÿ]", "", term)
        if not letters:
            continue
        score = 0
        # 1) exact substring (case insensitive) — strong signal
        if term.lower() in norm_compact or letters.lower() in norm_compact.replace(" ", ""):
            score += 10
        # 2) spelled-out: each letter appears as a single-letter word
        if all(letter.lower() in word_set for letter in letters):
            score += 5
        # 3) common letter pair appears
        if len(letters) >= 2 and letters[:2].lower() in norm_compact:
            score += 1
        if score > 0:
            candidates.append((score, term))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    selected = sorted({t for _, t in candidates[:max_terms]})
    return selected


# ─── Glossaire utilisateur (§5c du plan meeting-prep v2) ─────────


def load_user_glossary(user_sub: str, db) -> set:
    """Charge le glossaire utilisateur depuis ``user_glossary_terms``.

    Cap 300 termes (limite plus haute = bruit + augmente la latence du
    ``filter_relevant`` en aval). Exclut ``blacklisted = TRUE``. Tri
    ``(occurrence_count DESC, last_seen_at DESC)`` pour favoriser les
    termes les plus utilisés.

    ``db`` est une SQLAlchemy session déjà ouverte par le caller (typiquement
    file-puller). Best-effort : si la table n'existe pas (migration non
    appliquée), retourne un set vide en logguant un warning.
    """
    try:
        from sqlalchemy import text as _sql_text
        rows = db.execute(
            _sql_text(
                "SELECT term FROM user_glossary_terms "
                "WHERE user_sub = :u AND NOT blacklisted "
                "ORDER BY occurrence_count DESC, last_seen_at DESC "
                "LIMIT 300"
            ),
            {"u": user_sub},
        ).fetchall()
        return {r[0] for r in rows}
    except Exception as exc:
        logger.warning("load_user_glossary failed for user=%s: %s", user_sub, exc)
        return set()
