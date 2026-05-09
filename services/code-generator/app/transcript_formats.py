"""Format the kevent meeting-intelligence outputs into user-friendly downloads.

Conversions :
  - meeting_analysis_json (dict) → Markdown (5 sections)
  - text or Markdown → .docx (python-docx)
  - text or Markdown → .odt  (odfpy)

Markdown is the lingua franca: every other format is generated from a
plain-text or Markdown source. We deliberately keep the conversions naive
(headings, paragraphs, bullet lists) — fancy styling lives in downstream
tools like LibreOffice if the user wants to polish.

All converters are best-effort : if the optional dependency is missing, they
raise `ImportError` and the caller returns 503 to the UI.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any


# ─── Markdown formatter for meeting analysis JSON ───────────────────────────

_SECTION_TITLES = {
    "actors": "Acteurs présents",
    "themes": "Thématiques abordées",
    "decisions": "Décisions et points en action",
    "gaps": "Sujets non abordés",
    "recommendations": "Recommandations",
}


def _stringify(value: Any) -> str:
    """Convert a JSON-ish value to a human-readable single line."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)) or value is None:
        return "" if value is None else str(value)
    if isinstance(value, dict):
        # Render dicts as "key: value" pairs separated by " — "
        parts = []
        for k, v in value.items():
            sv = _stringify(v)
            if not sv or sv.lower() in ("none", "null"):
                continue
            parts.append(f"{k}: {sv}")
        return " — ".join(parts)
    if isinstance(value, list):
        return ", ".join(_stringify(v) for v in value if _stringify(v))
    return str(value)


def meeting_analysis_to_markdown(analysis: dict | str) -> str:
    """Format the 5-section structured analysis as readable Markdown.

    Accepts either the parsed dict or a JSON string. Sections that are absent
    or empty are skipped (no empty headings).
    """
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except (TypeError, ValueError):
            return analysis  # already plain text — pass through
    if not isinstance(analysis, dict):
        return ""
    out: list[str] = ["# Compte-rendu de réunion\n"]
    for key, title in _SECTION_TITLES.items():
        items = analysis.get(key)
        if not items:
            continue
        out.append(f"\n## {title}\n")
        if isinstance(items, list):
            for it in items:
                line = _stringify(it)
                if line:
                    out.append(f"- {line}\n")
        else:
            line = _stringify(items)
            if line:
                out.append(f"{line}\n")
    return "".join(out).rstrip() + "\n"


# ─── DOCX / ODT writers ─────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")


def _iter_blocks(text: str):
    """Yield (kind, level, content) tuples — kind ∈ {heading, bullet, para, blank}.

    Naive Markdown subset: # headings, - bullets, blank lines, paragraphs.
    """
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            yield ("blank", 0, "")
            continue
        m = _HEADING_RE.match(line)
        if m:
            yield ("heading", len(m.group(1)), m.group(2).strip())
            continue
        m = _BULLET_RE.match(line)
        if m:
            yield ("bullet", 0, m.group(1).strip())
            continue
        yield ("para", 0, line)


def text_to_docx_bytes(text: str, title: str | None = None) -> bytes:
    """Render a Markdown-ish string as a .docx blob."""
    from docx import Document  # python-docx
    doc = Document()
    if title:
        doc.core_properties.title = title
    last_kind = "blank"
    for kind, level, content in _iter_blocks(text):
        if kind == "heading":
            doc.add_heading(content, level=min(level, 4))
        elif kind == "bullet":
            doc.add_paragraph(content, style="List Bullet")
        elif kind == "para":
            doc.add_paragraph(content)
        # blank lines just reset the join, they don't add an empty para
        last_kind = kind
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def text_to_odt_bytes(text: str, title: str | None = None) -> bytes:
    """Render a Markdown-ish string as a .odt blob."""
    from odf.opendocument import OpenDocumentText  # odfpy
    from odf.style import Style, TextProperties, ParagraphProperties
    from odf.text import H, P, List, ListItem

    doc = OpenDocumentText()
    if title:
        doc.meta.addElement(_odt_meta_title(title))
    for kind, level, content in _iter_blocks(text):
        if kind == "heading":
            doc.text.addElement(H(outlinelevel=min(level, 4), text=content))
        elif kind == "bullet":
            lst = List()
            item = ListItem()
            item.addElement(P(text=content))
            lst.addElement(item)
            doc.text.addElement(lst)
        elif kind == "para":
            doc.text.addElement(P(text=content))
        # blank → skip
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _odt_meta_title(title: str):
    """Helper to build a <dc:title> element for ODT meta."""
    from odf.dc import Title  # odfpy
    el = Title()
    el.addText(title)
    return el
