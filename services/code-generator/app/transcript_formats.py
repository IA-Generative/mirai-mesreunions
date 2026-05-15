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
    # Nouvelles clés (B7) — séparation présents / cités. On garde la clé
    # legacy "actors" comme fallback rétro-compatible si participants_presents
    # et participants_cites sont tous deux absents/vides.
    "participants_presents": "Participants présents",
    "participants_cites": "Personnes citées",
    # Meeting-prep v2 §8 : la clé legacy "actors" est rendue sous "Acteurs
    # présents" pour aligner avec les tests / l'UX (anciens CRs B7-) ; les
    # nouveaux CRs utilisent participants_presents/participants_cites.
    "actors": "Acteurs présents",
    "themes": "Thématiques abordées",
    "decisions": "Décisions et points en action",
    "gaps": "Sujets non abordés",
    "recommendations": "Recommandations",
}


def _stringify(value: Any) -> str:
    """Fallback : convertit une valeur en ligne lisible.

    Meeting-prep v2 §8 : ne dump JAMAIS les clés JSON en clair (``name:``,
    ``role:``, etc.) — cause originale du bug d'affichage du CR. Pour les
    dicts, on s'appuie sur les renderers par section (cf
    ``_render_*``) ; ce fallback est utilisé uniquement pour des dicts non
    structurés rencontrés à l'usage. Heuristique : on extrait les valeurs,
    pas les clés.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)) or value is None:
        return "" if value is None else str(value)
    if isinstance(value, dict):
        # Concatène les valeurs scalaires, ignore les clés (évite "name:").
        parts = []
        for v in value.values():
            sv = _stringify(v)
            if sv and sv.lower() not in ("none", "null"):
                parts.append(sv)
        return " — ".join(parts)
    if isinstance(value, list):
        return ", ".join(_stringify(v) for v in value if _stringify(v))
    return str(value)


# ─── Renderers par section (§8 du plan) ─────────────────────────────────────


def _render_participant(item: Any) -> str:
    """Rendu d'un participant : ``Nom (Rôle)`` ou ``Nom — Contexte`` si cités."""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return _stringify(item)
    name = (item.get("name") or "").strip()
    role = (item.get("role") or "").strip()
    context = (item.get("context") or "").strip()
    parts = [name] if name else []
    if role:
        parts[-1] = f"{name} ({role})" if name else f"({role})"
    if context:
        parts.append(context)
    return " — ".join(p for p in parts if p)


def _render_theme(item: Any) -> str:
    """Rendu d'un thème : ``**Titre** — Résumé``."""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return _stringify(item)
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    if title and summary:
        return f"**{title}** — {summary}"
    return title or summary or _stringify(item)


def _render_decision(item: Any) -> str:
    """Rendu d'une décision : ``Item (👤 Owner, ⏰ Due)``."""
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return _stringify(item)
    text = (item.get("item") or item.get("summary") or item.get("title") or "").strip()
    owner = (item.get("owner") or "").strip()
    due = (item.get("due") or "").strip()
    suffix_parts: list[str] = []
    if owner:
        suffix_parts.append(f"👤 {owner}")
    if due:
        suffix_parts.append(f"⏰ {due}")
    if suffix_parts and text:
        return f"{text} ({', '.join(suffix_parts)})"
    return text or _stringify(item)


def _render_plain(item: Any) -> str:
    """Rendu d'une recommandation / gap : valeur seule, pas de clé."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        # Préférence : item ou summary > toute autre clé.
        for k in ("item", "summary", "title", "name"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return _stringify(item)
    return _stringify(item)


_SECTION_RENDERERS = {
    "participants_presents": _render_participant,
    "participants_cites": _render_participant,
    "actors": _render_participant,
    "themes": _render_theme,
    "decisions": _render_decision,
    "gaps": _render_plain,
    "recommendations": _render_plain,
}


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
    # Si les nouvelles clés (B7) sont présentes et non vides, on saute la
    # clé legacy "actors" (redondante). Sinon on garde "actors" pour les
    # anciens compte-rendus en DB générés avant le déploiement de B7.
    has_new_participants = bool(
        analysis.get("participants_presents") or analysis.get("participants_cites")
    )
    for key, title in _SECTION_TITLES.items():
        if key == "actors" and has_new_participants:
            continue  # skip legacy, on a déjà séparé en présents/cités
        items = analysis.get(key)
        if not items:
            continue
        out.append(f"\n## {title}\n")
        renderer = _SECTION_RENDERERS.get(key, _render_plain)
        if isinstance(items, list):
            for it in items:
                line = renderer(it)
                if line:
                    out.append(f"- {line}\n")
        else:
            line = renderer(items)
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
