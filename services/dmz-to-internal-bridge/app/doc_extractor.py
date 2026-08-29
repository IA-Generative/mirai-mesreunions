"""
Text extraction for the meeting-prep route — turns binary office documents
into plain text that the LLM can ingest.

The upstream flow is::

    DriveClient.download_item(...) -> (bytes, content_type)
                                              |
                                              v
                          extract_text(bytes, content_type, filename)
                                              |
                                              v
                                  plain text (str, possibly empty)

Supported formats:

  - PDF  (application/pdf)                            via pypdf
  - DOCX (…wordprocessingml.document)                 via python-docx
  - ODT  (…opendocument.text)                         via odfpy
  - PPTX (…presentationml.presentation)               via python-pptx
  - ODP  (…opendocument.presentation)                 via odfpy
  - Plain text / Markdown (text/plain, text/markdown) passthrough

Anything else is silently skipped (returns ``""``). The function NEVER
raises on a corrupted, encrypted or image-only document: those are normal
inputs in the wild and the caller just iterates over a folder. A warning
is logged so operators can see which docs were dropped.

Format detection prefers the MIME type when it is specific; it falls
back to the filename extension when MIME is generic (``application/
octet-stream``) — which happens with some S3 presigned responses.

A soft size guard (``max_chars``) lets the caller cap the per-document
text so a single 400-page PDF doesn't dominate the LLM context. The
caller is responsible for the global cap across multiple documents.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ─── MIME / extension routing table ──────────────────────────────
#
# ⚠ Mirrored in the browser by ``services/mesreunions-web/frontend/lib/
# source-basket.js`` (``MIME_TO_KIND`` / ``EXT_TO_KIND`` / ``GENERIC_MIMES``),
# so the Drive picker can grey out a file *before* downloading it. The two
# tables must move together: adding a format here without adding it there
# makes it unreachable from the picker; removing one there without removing it
# here lets users pick files that will silently yield no text.

# Specific MIME types we know how to read.
_MIME_TO_KIND = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.oasis.opendocument.text": "odt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.oasis.opendocument.presentation": "odp",
    "text/plain": "text",
    "text/markdown": "text",
    "text/x-markdown": "text",
}

# Filename extension fallback — used only when MIME is generic.
_EXT_TO_KIND = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".odt": "odt",
    ".pptx": "pptx",
    ".odp": "odp",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
}

# MIME types we treat as "generic" — i.e. trust the extension instead.
_GENERIC_MIMES = {"application/octet-stream", "binary/octet-stream", ""}


def _detect_kind(content_type: str, filename_hint: Optional[str]) -> Optional[str]:
    """Return one of pdf|docx|odt|pptx|odp|text, or None if unsupported."""
    # Trim parameters like "; charset=utf-8".
    bare_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if bare_mime and bare_mime not in _GENERIC_MIMES:
        kind = _MIME_TO_KIND.get(bare_mime)
        if kind:
            return kind
        # MIME was specific but unknown to us — no point trying the extension.
        return None
    if filename_hint:
        lower = filename_hint.lower()
        for ext, kind in _EXT_TO_KIND.items():
            if lower.endswith(ext):
                return kind
    return None


# ─── Public API ──────────────────────────────────────────────────

def extract_text(
    content: bytes,
    content_type: str,
    filename_hint: Optional[str] = None,
    max_chars: Optional[int] = None,
) -> str:
    """
    Extract plain text from a document.

    Always returns a string — empty if the format is unsupported, the
    file is corrupted/encrypted, or the document contains no text
    (e.g. a scanned PDF with no OCR layer).

    ``max_chars`` caps the returned text length; if exceeded the result
    is truncated and suffixed with ``"\\n[…truncated]"`` so the LLM sees
    that more content existed.
    """
    if not content:
        return ""
    kind = _detect_kind(content_type, filename_hint)
    if kind is None:
        logger.info(
            "doc_extractor: unsupported document mime=%r filename=%r — skipped",
            content_type, filename_hint,
        )
        return ""

    try:
        if kind == "pdf":
            text = _extract_pdf(content)
        elif kind == "docx":
            text = _extract_docx(content)
        elif kind == "odt":
            text = _extract_odf(content)
        elif kind == "pptx":
            text = _extract_pptx(content)
        elif kind == "odp":
            text = _extract_odf(content)
        elif kind == "text":
            text = _extract_text(content)
        else:  # unreachable thanks to _detect_kind, kept for safety
            return ""
    except Exception as exc:
        logger.warning(
            "doc_extractor: %s extraction failed for filename=%r: %s",
            kind, filename_hint, exc,
        )
        return ""

    text = _normalize(text)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[…truncated]"
    return text


# ─── Per-format readers ──────────────────────────────────────────

def _extract_pdf(content: bytes) -> str:
    # pypdf is imported lazily so unit tests that don't touch PDF code
    # can run without the dependency installed.
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
    except PdfReadError as exc:
        logger.warning("doc_extractor: pdf header invalid: %s", exc)
        return ""

    if getattr(reader, "is_encrypted", False):
        # Try the empty password — many PDFs are "encrypted" with no real
        # protection (e.g. set by Acrobat to disable copy-paste).
        try:
            reader.decrypt("")
        except Exception:
            logger.info("doc_extractor: pdf is encrypted and password unknown — skipped")
            return ""

    parts: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception as exc:
            logger.debug("doc_extractor: pdf page extract_text failed: %s", exc)
            page_text = ""
        if page_text:
            parts.append(page_text)
    return "\n\n".join(parts)


def _extract_docx(content: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(content))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text:
            parts.append(para.text)
    # Tables hold key information in many prep docs (action items, decisions).
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_odf(content: bytes) -> str:
    """Reader for both ODT and ODP — same odfpy ``teletype`` extraction."""
    from odf.opendocument import load
    from odf import teletype, text as odf_text, draw

    doc = load(io.BytesIO(content))
    parts: list[str] = []

    # Paragraphs (ODT body, ODP slide text content).
    for node in doc.getElementsByType(odf_text.P):
        chunk = teletype.extractText(node)
        if chunk:
            parts.append(chunk)
    # Headings (titles in ODT, slide titles in ODP wrapped in <draw:frame>).
    for node in doc.getElementsByType(odf_text.H):
        chunk = teletype.extractText(node)
        if chunk:
            parts.append(chunk)
    # ODP slide titles via <draw:text-box> — sometimes outside of <text:p>.
    for node in doc.getElementsByType(draw.TextBox):
        chunk = teletype.extractText(node)
        if chunk and chunk not in parts:  # dedupe against paragraphs already captured
            parts.append(chunk)

    return "\n".join(parts)


def _extract_pptx(content: bytes) -> str:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(content))
    parts: list[str] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                line = "".join(run.text for run in para.runs)
                if line.strip():
                    parts.append(line)
        # Speaker notes are gold for prep — they often hold the actual talking points.
        notes = getattr(slide, "notes_slide", None)
        if notes and notes.notes_text_frame and notes.notes_text_frame.text.strip():
            parts.append(notes.notes_text_frame.text.strip())
    return "\n".join(parts)


def _extract_text(content: bytes) -> str:
    # Try UTF-8 first (the only encoding that should ever come out of the
    # Drive). Fall back to latin-1 which never raises — better to render
    # mojibake than to drop the document entirely.
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


# ─── Post-processing ─────────────────────────────────────────────

def _normalize(text: str) -> str:
    """
    Tighten whitespace so the LLM doesn't waste tokens on blank lines.

    - collapse runs of 3+ newlines down to 2
    - strip trailing whitespace on each line
    - strip leading/trailing whitespace on the whole document
    """
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blank_streak = 0
    for line in lines:
        if line == "":
            blank_streak += 1
            if blank_streak <= 1:
                out.append("")
        else:
            blank_streak = 0
            out.append(line)
    return "\n".join(out).strip()
