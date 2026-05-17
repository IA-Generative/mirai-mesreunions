"""Module mailer — envoi SMTP du CR aux participants (Lot 8 PR-6).

Wrapper minimal autour de ``smtplib`` qui :

- Lit la config SMTP depuis l'environnement (``SMTP_HOST``, ``SMTP_PORT``,
  ``SMTP_USER``, ``SMTP_PASSWORD``, ``SMTP_FROM``, ``SMTP_USE_TLS``).
- Expose ``is_configured()`` pour qu'un endpoint puisse répondre 503 propre
  si la config manque (mode "dry-run" / "preview").
- Expose ``send_meeting_cr_email(...)`` qui :
    * Charge ``Meeting`` + ``Preparation`` via le device-token-authority
      (``request_internal_preparation_api`` / ``request_internal_meeting_api``).
    * Construit un body texte court (sujet + résumé + lien direct).
    * Envoie en best-effort (loggue + retourne ``{ok, sent, skipped, error}``).

Conception : aucun crash si SMTP_HOST manquant ou DNS down — on retourne un
dict de statut. C'est l'appelant (hook post-transcription) qui décide.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable

logger = logging.getLogger("mesreunions_web.mailer")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _truthy(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def get_config() -> dict:
    """Retourne la config SMTP courante (sans le password) pour diagnostic."""
    return {
        "host": _env("SMTP_HOST"),
        "port": int(_env("SMTP_PORT", "587") or "587"),
        "user": _env("SMTP_USER"),
        "from": _env("SMTP_FROM") or _env("SMTP_USER"),
        "use_tls": _truthy("SMTP_USE_TLS", default=True),
        "configured": bool(_env("SMTP_HOST") and _env("SMTP_FROM") or _env("SMTP_USER")),
    }


def is_configured() -> bool:
    """True ssi SMTP_HOST + un From (SMTP_FROM ou SMTP_USER) sont définis."""
    return bool(_env("SMTP_HOST") and (_env("SMTP_FROM") or _env("SMTP_USER")))


def _build_message(
    *,
    to_addrs: list[str],
    subject: str,
    body_text: str,
    from_addr: str,
    reply_to: str | None = None,
    attachments: Iterable[tuple[str, bytes, str]] | None = None,
) -> EmailMessage:
    """Construit un EmailMessage texte (+ pièces jointes éventuelles)."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body_text)
    for filename, data, mime in (attachments or []):
        maintype, _, subtype = mime.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def _send_smtp(msg: EmailMessage) -> None:
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587") or "587")
    user = _env("SMTP_USER")
    password = _env("SMTP_PASSWORD")
    use_tls = _truthy("SMTP_USE_TLS", default=True)
    timeout = int(_env("SMTP_TIMEOUT", "20") or "20")

    if use_tls:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)


def _valid_emails(participants: list | None) -> list[str]:
    out: list[str] = []
    if not isinstance(participants, list):
        return out
    seen: set[str] = set()
    for p in participants:
        if not isinstance(p, dict):
            continue
        email = (p.get("email") or "").strip()
        if not email or "@" not in email:
            continue
        lc = email.lower()
        if lc in seen:
            continue
        seen.add(lc)
        out.append(email)
    return out


def _short_summary(meeting: dict) -> str:
    """Résumé court (1ère portion du summary ou des absentee/key points)."""
    s = (meeting.get("summary") or "").strip()
    if s:
        return s[:600]
    content = meeting.get("content") or {}
    if isinstance(content, dict):
        ab = (content.get("absentee_summary") or "").strip()
        if ab:
            return ab[:600]
    return "Le compte rendu est disponible dans l'application Mes Réunions."


def build_cr_email(
    *,
    meeting: dict,
    preparation: dict | None,
    public_base_url: str,
) -> tuple[str, str]:
    """Construit (subject, body_text) pour un email CR.

    Visible-only : ne nécessite pas de SMTP configuré (utilisable côté UI
    pour preview).
    """
    title = (
        (preparation or {}).get("title")
        or meeting.get("title")
        or "Réunion"
    )
    when = meeting.get("created_at") or ""
    summary = _short_summary(meeting)
    base = (public_base_url or "").rstrip("/")
    mid = meeting.get("id") or ""
    link = f"{base}/?tab=reunion&meeting_id={mid}" if base and mid else ""
    subject = f"Compte rendu : {title}"
    lines = [
        f"Bonjour,",
        "",
        f"Le compte rendu de la réunion « {title} » est disponible.",
    ]
    if when:
        lines.append(f"Date : {when}")
    lines.extend([
        "",
        "Résumé :",
        summary,
        "",
    ])
    if link:
        lines.append(f"Accéder au CR complet : {link}")
        lines.append("")
    lines.append("— Envoyé automatiquement par Mes Réunions")
    return subject, "\n".join(lines)


def send_meeting_cr_email(
    *,
    meeting: dict,
    preparation: dict | None,
    recipients: list[str] | None = None,
    public_base_url: str | None = None,
    attachments: Iterable[tuple[str, bytes, str]] | None = None,
    reply_to: str | None = None,
) -> dict:
    """Envoie le CR aux ``recipients`` (ou aux participants[].email de la prep).

    Retourne ``{ok: bool, sent: int, skipped_reason?: str, error?: str}``.
    Best-effort : ne lève jamais.
    """
    if not is_configured():
        return {"ok": False, "sent": 0, "skipped_reason": "smtp_not_configured"}
    to_addrs = list(recipients or _valid_emails((preparation or {}).get("participants")))
    if not to_addrs:
        return {"ok": False, "sent": 0, "skipped_reason": "no_recipient"}
    base = public_base_url or _env("PUBLIC_BASE_URL")
    subject, body = build_cr_email(
        meeting=meeting, preparation=preparation, public_base_url=base,
    )
    from_addr = _env("SMTP_FROM") or _env("SMTP_USER")
    try:
        msg = _build_message(
            to_addrs=to_addrs,
            subject=subject,
            body_text=body,
            from_addr=from_addr,
            reply_to=reply_to,
            attachments=attachments,
        )
        _send_smtp(msg)
        logger.info(
            "mailer: sent CR email meeting=%s to %d recipients",
            meeting.get("id"), len(to_addrs),
        )
        return {"ok": True, "sent": len(to_addrs)}
    except Exception as exc:
        logger.exception(
            "mailer: failed to send CR email meeting=%s", meeting.get("id"),
        )
        return {"ok": False, "sent": 0, "error": str(exc)}
