"""Lot 4 — Export d'une préparation en DOCX / ODT.

La structure de ``preparation.content`` (JSON) est définie par
``meeting_prep.py`` et consommée côté front par ``renderBriefBody`` dans
``frontend/tabs/preparations.js``. On reproduit ici la **même séquence
de sections** pour que les exports (txt/md/docx/odt) restent fidèles à
l'écran :

  1. Objectif & contexte           (objective_reformulated + context_recap)
  2. Ordre du jour                 (agenda[] : title + duration + objective + key_questions)
  3. Participants                  (prep.participants[] : name + email + role + note)
  4. Points en suspens             (open_threads[] : item + source)
  5. Questions d'ouverture         (opening_questions[])
  6. Points de vigilance           (risk_points[])
  7. À faire avant la réunion      (preparation_checklist[])

TXT et MD sont sérialisés côté front (``lib/export-formatter.js``) pour
éviter un aller-retour réseau inutile ; DOCX et ODT nécessitent
``python-docx`` et ``odfpy`` (déjà présents dans ``requirements.txt``).
"""

from __future__ import annotations

import io
import re
from typing import Any

# ─── Helpers communs ──────────────────────────────────────────────────


def slugify(value: str, *, max_len: int = 60) -> str:
    """Slug ASCII safe pour ``Content-Disposition``.

    - lowercase, accents stripés (best-effort via NFKD)
    - non-alphanum → tiret
    - séquences de tirets compactées, trim
    - fallback ``"preparation"`` si vide
    """
    import unicodedata

    s = unicodedata.normalize("NFKD", value or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "preparation"


def _sections(prep: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """Renvoie ``[(emoji, titre, payload), ...]`` non-vide.

    ``payload`` est laissé brut (str ou list) — les renderers DOCX/ODT
    et le front interprètent selon le type.

    La section Participants est sourcée depuis ``prep.participants`` (la
    liste éditable saisie par l'utilisateur, avec email + note libre),
    PAS depuis ``brief_json.participants_notes`` (champ LLM ignoré au
    rendu).
    """
    prep = prep or {}
    bj = prep.get("content") or {}
    out: list[tuple[str, str, Any]] = []

    objective = (bj.get("objective_reformulated") or "").strip()
    context = (bj.get("context_recap") or "").strip()
    if objective or context:
        out.append(("🎯", "Objectif & contexte",
                    {"objective": objective, "context": context}))

    agenda = [it for it in (bj.get("agenda") or []) if isinstance(it, dict)]
    if agenda:
        out.append(("📋", "Ordre du jour", agenda))

    participants = [
        p for p in (prep.get("participants") or []) if isinstance(p, dict)
    ]
    if participants:
        out.append(("👥", "Participants", participants))

    threads = [t for t in (bj.get("open_threads") or []) if isinstance(t, dict)]
    if threads:
        out.append(("🧵", "Points en suspens", threads))

    opening = [q for q in (bj.get("opening_questions") or []) if (q or "").strip()]
    if opening:
        out.append(("💬", "Questions d'ouverture", opening))

    risks = [q for q in (bj.get("risk_points") or []) if (q or "").strip()]
    if risks:
        out.append(("⚠️", "Points de vigilance", risks))

    checklist = [q for q in (bj.get("preparation_checklist") or []) if (q or "").strip()]
    if checklist:
        out.append(("✅", "À faire avant la réunion", checklist))

    # Recommandations IA (suggestions humbles) — aplaties en chaînes pour
    # réutiliser la branche « liste simple » des deux renderers binaires.
    recos: list[str] = []
    for r in (bj.get("ai_recommendations") or []):
        if not isinstance(r, dict):
            continue
        suggestion = (r.get("suggestion") or "").strip()
        if not suggestion:
            continue
        rationale = (r.get("rationale") or "").strip()
        recos.append(f"{suggestion} ({rationale})" if rationale else suggestion)
    if recos:
        out.append(("💡", "Recommandations", recos))

    return out


def _header_lines(prep: dict) -> tuple[str, list[str]]:
    """Retourne ``(titre, lignes_meta)`` du header (commun docx/odt)."""
    title = (prep.get("title") or prep.get("subject") or "Préparation de réunion").strip()
    meta: list[str] = []
    created = (prep.get("created_at") or "")[:16].replace("T", " ")
    if created:
        meta.append(f"Créé le {created}")
    role = (prep.get("role") or "").strip()
    if role:
        meta.append(f"Rôle : {role}")
    dur = prep.get("duration_minutes")
    if dur:
        meta.append(f"Durée prévue : {dur} min")
    return title, meta


# ─── Renderer DOCX (python-docx) ──────────────────────────────────────


def render_docx(prep: dict) -> bytes:
    """Génère un .docx sobre DSFR-like (Marianne unavailable → fallback).

    Style : titres en gras taille croissante, listes à puces / numérotées,
    paragraphes justifiés gauche. Pas de couleurs flashy.
    """
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()

    # Marges et police par défaut (sobre, lisible)
    try:
        normal = doc.styles["Normal"]
        normal.font.name = "Calibri"  # fallback cross-platform
        normal.font.size = Pt(11)
    except Exception:
        pass

    title, meta = _header_lines(prep)

    h = doc.add_paragraph()
    run = h.add_run(title)
    run.bold = True
    run.font.size = Pt(20)

    if meta:
        m = doc.add_paragraph()
        mr = m.add_run(" · ".join(meta))
        mr.italic = True
        mr.font.size = Pt(9)
        mr.font.color.rgb = RGBColor(0x4B, 0x55, 0x63)

    doc.add_paragraph()  # spacer

    for emoji, section_title, payload in _sections(prep):
        hp = doc.add_paragraph()
        hr = hp.add_run(f"{emoji}  {section_title}")
        hr.bold = True
        hr.font.size = Pt(14)

        if section_title.startswith("Objectif"):
            obj = (payload or {}).get("objective") or ""
            ctx = (payload or {}).get("context") or ""
            if obj:
                doc.add_paragraph(obj)
            if ctx:
                p = doc.add_paragraph()
                r = p.add_run(ctx)
                r.italic = True

        elif section_title == "Ordre du jour":
            for idx, item in enumerate(payload, start=1):
                t = (item.get("title") or "(sans titre)").strip()
                dur = item.get("duration_minutes")
                dur_str = f" ({int(dur)} min)" if dur else ""
                p = doc.add_paragraph(style="List Number")
                r = p.add_run(f"{t}{dur_str}")
                r.bold = True
                objv = (item.get("objective") or "").strip()
                if objv:
                    sp = doc.add_paragraph(objv)
                    sp.paragraph_format.left_indent = Pt(20)
                kqs = [q for q in (item.get("key_questions") or []) if (q or "").strip()]
                for q in kqs:
                    qp = doc.add_paragraph(q, style="List Bullet")
                    qp.paragraph_format.left_indent = Pt(20)

        elif section_title == "Participants":
            for p_dict in payload:
                name = (p_dict.get("name") or "—").strip()
                email = (p_dict.get("email") or "").strip()
                role = (p_dict.get("role") or "").strip()
                note = (p_dict.get("note") or "").strip()
                bp = doc.add_paragraph(style="List Bullet")
                br = bp.add_run(name)
                br.bold = True
                meta_bits = [b for b in (role, email) if b]
                if meta_bits:
                    mr = bp.add_run(f" ({' · '.join(meta_bits)})")
                    mr.font.size = Pt(9)
                    mr.font.color.rgb = RGBColor(0x4B, 0x55, 0x63)
                if note:
                    bp.add_run(f" — {note}")

        elif section_title == "Points en suspens":
            for t_dict in payload:
                item = (t_dict.get("item") or "").strip()
                if not item:
                    continue
                src = (t_dict.get("source") or "").strip()
                bp = doc.add_paragraph(style="List Bullet")
                bp.add_run(item)
                if src:
                    sr = bp.add_run(f"  (source : {src})")
                    sr.italic = True
                    sr.font.size = Pt(9)

        else:  # listes simples (str)
            for s in payload:
                doc.add_paragraph(str(s), style="List Bullet")

        doc.add_paragraph()  # spacer

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ─── Renderer ODT (odfpy) ─────────────────────────────────────────────


def render_odt(prep: dict) -> bytes:
    """Génère un .odt sobre via odfpy.

    On n'utilise QUE des styles ODT standards (Heading 1/2, Text body,
    List Bullet) afin de garantir l'ouverture sur LibreOffice / Word
    sans warning de style manquant.
    """
    from odf.opendocument import OpenDocumentText
    from odf.style import (
        Style, TextProperties, ParagraphProperties,
    )
    from odf.text import H, P, Span, List, ListItem

    doc = OpenDocumentText()

    # ── Styles maison sobres (gris très foncé, sans couleurs flashy) ──
    title_style = Style(name="MCRTitle", family="paragraph")
    title_style.addElement(TextProperties(fontsize="20pt", fontweight="bold"))
    title_style.addElement(ParagraphProperties(marginbottom="0.2cm"))
    doc.styles.addElement(title_style)

    meta_style = Style(name="MCRMeta", family="paragraph")
    meta_style.addElement(TextProperties(fontsize="9pt", fontstyle="italic", color="#4B5563"))
    meta_style.addElement(ParagraphProperties(marginbottom="0.6cm"))
    doc.styles.addElement(meta_style)

    h2_style = Style(name="MCRSection", family="paragraph")
    h2_style.addElement(TextProperties(fontsize="14pt", fontweight="bold"))
    h2_style.addElement(ParagraphProperties(margintop="0.5cm", marginbottom="0.2cm"))
    doc.styles.addElement(h2_style)

    body_style = Style(name="MCRBody", family="paragraph")
    body_style.addElement(TextProperties(fontsize="11pt"))
    body_style.addElement(ParagraphProperties(marginbottom="0.15cm"))
    doc.styles.addElement(body_style)

    italic_style = Style(name="MCRItalic", family="text")
    italic_style.addElement(TextProperties(fontstyle="italic"))
    doc.styles.addElement(italic_style)

    bold_style = Style(name="MCRBold", family="text")
    bold_style.addElement(TextProperties(fontweight="bold"))
    doc.styles.addElement(bold_style)

    small_style = Style(name="MCRSmall", family="text")
    small_style.addElement(TextProperties(fontsize="9pt", fontstyle="italic", color="#4B5563"))
    doc.styles.addElement(small_style)

    title, meta = _header_lines(prep)
    doc.text.addElement(H(outlinelevel=1, stylename=title_style, text=title))
    if meta:
        doc.text.addElement(P(stylename=meta_style, text=" · ".join(meta)))

    for emoji, section_title, payload in _sections(prep):
        doc.text.addElement(H(outlinelevel=2, stylename=h2_style,
                              text=f"{emoji}  {section_title}"))

        if section_title.startswith("Objectif"):
            obj = (payload or {}).get("objective") or ""
            ctx = (payload or {}).get("context") or ""
            if obj:
                doc.text.addElement(P(stylename=body_style, text=obj))
            if ctx:
                p = P(stylename=body_style)
                p.addElement(Span(stylename=italic_style, text=ctx))
                doc.text.addElement(p)

        elif section_title == "Ordre du jour":
            lst = List()
            for item in payload:
                t = (item.get("title") or "(sans titre)").strip()
                dur = item.get("duration_minutes")
                dur_str = f" ({int(dur)} min)" if dur else ""
                li = ListItem()
                p = P(stylename=body_style)
                p.addElement(Span(stylename=bold_style, text=f"{t}{dur_str}"))
                li.addElement(p)
                objv = (item.get("objective") or "").strip()
                if objv:
                    li.addElement(P(stylename=body_style, text=objv))
                for q in (item.get("key_questions") or []):
                    q = (q or "").strip()
                    if q:
                        li.addElement(P(stylename=body_style, text=f"• {q}"))
                lst.addElement(li)
            doc.text.addElement(lst)

        elif section_title == "Participants":
            lst = List()
            for p_dict in payload:
                name = (p_dict.get("name") or "—").strip()
                email = (p_dict.get("email") or "").strip()
                role = (p_dict.get("role") or "").strip()
                note = (p_dict.get("note") or "").strip()
                li = ListItem()
                p = P(stylename=body_style)
                p.addElement(Span(stylename=bold_style, text=name))
                meta_bits = [b for b in (role, email) if b]
                if meta_bits:
                    p.addElement(Span(stylename=small_style,
                                      text=f" ({' · '.join(meta_bits)})"))
                if note:
                    p.addText(f" — {note}")
                li.addElement(p)
                lst.addElement(li)
            doc.text.addElement(lst)

        elif section_title == "Points en suspens":
            lst = List()
            for t_dict in payload:
                item = (t_dict.get("item") or "").strip()
                if not item:
                    continue
                src = (t_dict.get("source") or "").strip()
                li = ListItem()
                p = P(stylename=body_style, text=item)
                if src:
                    p.addElement(Span(stylename=small_style, text=f"  (source : {src})"))
                li.addElement(p)
                lst.addElement(li)
            doc.text.addElement(lst)

        else:
            lst = List()
            for s in payload:
                li = ListItem()
                li.addElement(P(stylename=body_style, text=str(s)))
                lst.addElement(li)
            doc.text.addElement(lst)

    buf = io.BytesIO()
    doc.write(buf)
    return buf.getvalue()


# ─── Dispatcher ───────────────────────────────────────────────────────


CONTENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
}


def render(prep: dict, fmt: str) -> tuple[bytes, str, str]:
    """Renvoie ``(bytes, content_type, filename)`` pour le format demandé.

    Raise ``ValueError`` si format non supporté.
    """
    fmt = (fmt or "").lower().strip()
    if fmt == "docx":
        data = render_docx(prep)
    elif fmt == "odt":
        data = render_odt(prep)
    else:
        raise ValueError(f"unsupported format: {fmt!r}")
    title = (prep.get("title") or prep.get("subject") or "preparation")
    filename = f"prep-{slugify(title)}.{fmt}"
    return data, CONTENT_TYPES[fmt], filename
