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
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
_SPEAKER_RE = re.compile(r"^\*\*([^*]+)\*\*\s*(_\([^)]+\)_)?\s*$")

# Inline runs : **bold** | _italic_ | *italic*. On parse en passe linéaire
# pour produire des segments (text, bold, italic) qu'on rejoue ensuite côté
# docx (Run) ou odt (Span). Pas de gestion imbriquée (rare en pratique sur
# nos transcriptions Whisper + LLM).
_INLINE_RE = re.compile(
    r"(\*\*([^*]+)\*\*)"      # **bold**
    r"|(\*([^*]+)\*)"          # *italic*
    r"|(_([^_]+)_)"            # _italic_
)


def _parse_inline_runs(text: str):
    """Yield (segment, bold, italic) tuples covering the whole `text`.

    Naïf : pas d'imbrication (le pipeline whisper/LLM ne produit pas de
    ``**_truc_**``). Les marqueurs non appariés sont laissés tels quels.
    """
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            yield (text[pos:m.start()], False, False)
        if m.group(2) is not None:
            yield (m.group(2), True, False)
        elif m.group(4) is not None:
            yield (m.group(4), False, True)
        elif m.group(6) is not None:
            yield (m.group(6), False, True)
        pos = m.end()
    if pos < len(text):
        yield (text[pos:], False, False)


def _iter_blocks(text: str):
    """Yield (kind, level, content) tuples — kind ∈ {heading, bullet,
    blockquote, speaker, para, blank}.

    Markdown subset reconnu :
      • ``# heading``  → (heading, level, content)
      • ``- bullet``   → (bullet, 0, content)
      • ``> quote``    → (blockquote, 0, content) — utilisé par speaker-tagged
      • ``**Nom** _(0:00 → 1:09)_`` → (speaker, 0, content) — speaker line
      • ligne vide     → (blank, 0, "")
      • autre          → (para, 0, content) — rendu avec runs inline
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
        m = _BLOCKQUOTE_RE.match(line)
        if m:
            yield ("blockquote", 0, m.group(1).strip())
            continue
        if _SPEAKER_RE.match(line):
            yield ("speaker", 0, line)
            continue
        yield ("para", 0, line)


def _docx_add_runs(paragraph, text: str):
    """Helper : ajoute les runs bold/italic dans un paragraphe DOCX."""
    for seg, bold, italic in _parse_inline_runs(text):
        if not seg:
            continue
        run = paragraph.add_run(seg)
        if bold:
            run.bold = True
        if italic:
            run.italic = True


def text_to_docx_bytes(text: str, title: str | None = None) -> bytes:
    """Render a Markdown-ish string as a .docx blob.

    Reconnaît bold (**...**), italic (_..._ / *...*), blockquote (>),
    speaker lines (**Nom** _(timecodes)_). Le speaker-tagged donne donc
    bien un bloc nom-en-gras + temps-en-italique + contenu en blockquote
    visuellement distinct.
    """
    from docx import Document  # python-docx
    doc = Document()
    if title:
        doc.core_properties.title = title
    last_kind = "blank"
    for kind, level, content in _iter_blocks(text):
        if kind == "heading":
            doc.add_heading(content, level=min(level, 4))
        elif kind == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            _docx_add_runs(p, content)
        elif kind == "blockquote":
            # Indenté visuellement via style "Quote" (présent dans tous
            # les templates DOCX par défaut). Garde l'inline parsing
            # (italique / gras dans une citation possible).
            p = doc.add_paragraph(style="Quote")
            _docx_add_runs(p, content)
        elif kind == "speaker":
            # Force un saut entre 2 locuteurs (1 blank avant si pas déjà
            # le cas). Le nom est en gras, le timecode entre parenthèses
            # en italique (les runs sont déjà produits par
            # _parse_inline_runs depuis **Nom** _(time)_).
            if last_kind not in ("blank", "speaker"):
                doc.add_paragraph()
            p = doc.add_paragraph()
            _docx_add_runs(p, content)
        elif kind == "para":
            p = doc.add_paragraph()
            _docx_add_runs(p, content)
        # blank lines just reset the join, they don't add an empty para
        last_kind = kind
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _odt_add_spans(parent_el, text: str):
    """Helper : ajoute les spans bold/italic dans un élément ODT (P, H, …)."""
    from odf.text import Span
    for seg, bold, italic in _parse_inline_runs(text):
        if not seg:
            continue
        if bold or italic:
            style_name = _odt_inline_style(bold, italic)
            sp = Span(stylename=style_name, text=seg)
            parent_el.addElement(sp)
        else:
            # Texte sans style → utilise addText pour rester compatible
            # avec le rendu LibreOffice par défaut.
            parent_el.addText(seg)


def _odt_inline_style(bold: bool, italic: bool) -> str:
    """Retourne le nom d'un style ODT inline (créé à la volée si besoin)."""
    parts = []
    if bold:
        parts.append("Bold")
    if italic:
        parts.append("Italic")
    return "".join(parts) if parts else "Default"


def _ensure_odt_inline_styles(doc):
    """Ajoute les styles Bold / Italic / BoldItalic au document s'ils
    n'existent pas déjà. Idempotent (re-appel = no-op)."""
    from odf.style import Style, TextProperties
    needed = [
        ("Bold", {"fontweight": "bold"}),
        ("Italic", {"fontstyle": "italic"}),
        ("BoldItalic", {"fontweight": "bold", "fontstyle": "italic"}),
    ]
    for name, props in needed:
        existing = [s for s in doc.automaticstyles.childNodes
                    if hasattr(s, "getAttribute") and s.getAttribute("name") == name]
        if existing:
            continue
        s = Style(name=name, family="text")
        s.addElement(TextProperties(**props))
        doc.automaticstyles.addElement(s)


def text_to_odt_bytes(text: str, title: str | None = None) -> bytes:
    """Render a Markdown-ish string as a .odt blob.

    Reconnaît les mêmes formes que ``text_to_docx_bytes``. Les spans
    bold/italic utilisent des styles inline injectés dans automaticstyles.
    """
    from odf.opendocument import OpenDocumentText  # odfpy
    from odf.text import H, P, List, ListItem

    doc = OpenDocumentText()
    if title:
        doc.meta.addElement(_odt_meta_title(title))
    _ensure_odt_inline_styles(doc)
    last_kind = "blank"
    for kind, level, content in _iter_blocks(text):
        if kind == "heading":
            h = H(outlinelevel=min(level, 4))
            _odt_add_spans(h, content)
            doc.text.addElement(h)
        elif kind == "bullet":
            lst = List()
            item = ListItem()
            p = P()
            _odt_add_spans(p, content)
            item.addElement(p)
            lst.addElement(item)
            doc.text.addElement(lst)
        elif kind == "blockquote":
            # ODT n'a pas de style "Quote" standardisé ; on indente via
            # un préfixe "« » " pour rester lisible, et on garde inline.
            p = P()
            _odt_add_spans(p, "« " + content + " »")
            doc.text.addElement(p)
        elif kind == "speaker":
            if last_kind not in ("blank", "speaker"):
                doc.text.addElement(P())   # blank line avant un nouveau locuteur
            p = P()
            _odt_add_spans(p, content)
            doc.text.addElement(p)
        elif kind == "para":
            p = P()
            _odt_add_spans(p, content)
            doc.text.addElement(p)
        # blank → skip (les sauts de paragraphes sont gérés au cas par cas)
        last_kind = kind
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ─── Plain text / Markdown normalizer (B3) ──────────────────────────────────

def text_to_plain_string(text: str) -> str:
    """Convertit une chaîne Markdown-ish en plain text propre.

    - Retire les marqueurs inline ``**``, ``_``, ``*`` (le contenu est gardé).
    - Convertit ``>`` (blockquote) en ligne indentée + paragraphe séparé.
    - Garantit 1 ligne vide entre chaque bloc + entre 2 speaker-lines.
    - Pour les bullets ``-`` : conserve le tiret (lisible en .txt).
    """
    out: list[str] = []
    last_kind = "blank"
    for kind, level, content in _iter_blocks(text):
        if kind == "blank":
            if out and out[-1] != "":
                out.append("")
            last_kind = "blank"
            continue
        # Strip MD inline pour garder uniquement le contenu textuel.
        clean = "".join(seg for seg, _b, _i in _parse_inline_runs(content))
        if kind == "heading":
            if out and out[-1] != "":
                out.append("")
            out.append(clean)
            # Soulignement ASCII discret sous le heading.
            out.append("─" * min(40, max(8, len(clean))))
        elif kind == "bullet":
            out.append(f"  • {clean}")
        elif kind == "blockquote":
            out.append(f"  {clean}")
        elif kind == "speaker":
            # Force blank avant un nouveau locuteur (sauf si déjà blank).
            if out and out[-1] != "":
                out.append("")
            out.append(clean)
        else:  # para
            out.append(clean)
        last_kind = kind
    # Normalise les blank lines consécutifs (max 1).
    norm: list[str] = []
    prev_blank = False
    for line in out:
        if line == "":
            if not prev_blank:
                norm.append(line)
            prev_blank = True
        else:
            norm.append(line)
            prev_blank = False
    # Trim trailing blank.
    while norm and norm[-1] == "":
        norm.pop()
    return "\n".join(norm) + "\n"


def text_to_md_string(text: str) -> str:
    """Normalise une chaîne Markdown-ish : garantit 1 ligne vide entre les
    blocs, sépare bien les speaker-lines. Conserve les marqueurs inline."""
    out: list[str] = []
    last_kind = "blank"
    for kind, level, content in _iter_blocks(text):
        if kind == "blank":
            if out and out[-1] != "":
                out.append("")
            last_kind = "blank"
            continue
        if kind == "heading":
            if out and out[-1] != "":
                out.append("")
            out.append(f"{'#' * level} {content}")
        elif kind == "bullet":
            out.append(f"- {content}")
        elif kind == "blockquote":
            out.append(f"> {content}")
        elif kind == "speaker":
            if out and out[-1] != "":
                out.append("")
            out.append(content)
        else:
            out.append(content)
        last_kind = kind
    norm: list[str] = []
    prev_blank = False
    for line in out:
        if line == "":
            if not prev_blank:
                norm.append(line)
            prev_blank = True
        else:
            norm.append(line)
            prev_blank = False
    while norm and norm[-1] == "":
        norm.pop()
    return "\n".join(norm) + "\n"


def _odt_meta_title(title: str):
    """Helper to build a <dc:title> element for ODT meta."""
    from odf.dc import Title  # odfpy
    el = Title()
    el.addText(title)
    return el
