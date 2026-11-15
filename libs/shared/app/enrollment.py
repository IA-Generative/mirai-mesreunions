"""Émission d'un code d'enrôlement d'appareil (QR + code court).

Cœur partagé entre ``mesreunions-web`` (session OIDC) et
``qr-enrollment-portal`` (identité Passage2) : seule la façon d'obtenir
l'utilisateur diffère, l'émission du jeton, l'écriture de la session
d'upload et l'enrichissement de la liste d'appareils sont identiques.

``user`` est un dict au format de la session OIDC de mesreunions-web :
``{"sub": ..., "email": ..., "name": ...}`` où ``sub`` est le subject
Keycloak auquel les fichiers déposés seront rattachés.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Iterable, Optional
from uuid import uuid4

import qrcode
import requests as req

from libs.shared.app.config import (
    INTERNAL_API_TOKEN,
    TOKEN_ISSUER_API_URL,
    UPLOAD_STATUS_VIEW_TTL_MINUTES,
)
from libs.shared.app.models import UploadSession, UploadTokenOption


def make_qr_png(url: str) -> bytes:
    """Encode ``url`` en QR code PNG (correction M, lisible sur écran)."""
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_url_for(base_url: str, qr_token: str) -> str:
    """URL encodée dans le QR : page d'upload de la DMZ.

    L'app mobile en extrait le ``qr_token`` (segment ``/upload/<token>``),
    la PWA l'ouvre telle quelle.
    """
    return f"{base_url.rstrip('/')}/upload/{qr_token}"


def request_token_from_internal(user: dict, ttl_minutes: int, max_uploads: int,
                                ttl_seconds: Optional[int] = None,
                                auto_transcribe: bool = True) -> dict:
    """Demande un jeton (``qr_token`` + ``simple_code``) à device-token-authority."""
    payload = {
        "user_sub": user["sub"],
        "user_email": user.get("email"),
        "user_display_name": user.get("name"),
        "ttl_minutes": ttl_minutes,
        "max_uploads": max_uploads,
        "auto_transcribe": bool(auto_transcribe),
    }
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds
    resp = req.post(
        TOKEN_ISSUER_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {INTERNAL_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def record_upload_session(db, user: dict, token_data: dict, *, ttl_minutes: int,
                          max_uploads: int, auto_transcribe: bool) -> UploadSession:
    """Écrit la session d'upload (zone externe) liée au jeton émis.

    La PWA d'upload et l'app mobile ne connaissent que le ``qr_token`` : c'est
    cette ligne qui rattache les fichiers déposés à ``user["sub"]``.
    """
    expires_at = datetime.fromisoformat(token_data["expires_at"])
    upload_session = UploadSession(
        id=uuid4(),
        user_sub=user["sub"],
        user_email=user.get("email"),
        user_display_name=user.get("name"),
        simple_code=token_data["simple_code"],
        qr_token=token_data["qr_token"],
        max_uploads=max_uploads,
        ttl_minutes=ttl_minutes,
        expires_at=expires_at,
        status_view_expires_at=expires_at + timedelta(minutes=UPLOAD_STATUS_VIEW_TTL_MINUTES),
    )
    db.add(upload_session)
    db.add(UploadTokenOption(
        id=uuid4(), qr_token=token_data["qr_token"],
        simple_code=token_data["simple_code"], auto_transcribe=auto_transcribe,
    ))
    db.commit()
    return upload_session


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def enrich_devices(devices: Iterable, sessions: Iterable[UploadSession],
                   now: Optional[datetime] = None) -> list:
    """Complète les appareils de device-token-authority avec leur session d'upload.

    Pour chaque appareil : dépôts des dernières 24 h, dépôts restants, code
    court et expiration de la session la plus récente du même ``qr_token``.
    """
    now = now or datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    session_by_qr = {}
    for s in sessions:
        token = (s.qr_token or "").strip()
        if not token:
            continue
        prev = session_by_qr.get(token)
        if prev is None or (s.created_at and prev.created_at and s.created_at > prev.created_at):
            session_by_qr[token] = s

    enriched = []
    for d in devices:
        item = dict(d) if isinstance(d, dict) else {}
        s = session_by_qr.get((item.get("qr_token") or "").strip())
        if s is None:
            item.update({
                "recent_uploads_24h": 0,
                "remaining_uploads": 0,
                "session_max_uploads": 0,
                "session_upload_count": 0,
                "session_simple_code": None,
                "session_expiring_soon": False,
                "session_expires_at": None,
            })
            enriched.append(item)
            continue

        recent = sum(
            1 for f in (s.uploads or [])
            if f.created_at is not None and _utc(f.created_at) >= day_ago
        )
        max_uploads = int(s.max_uploads or 0)
        upload_count = int(s.upload_count or 0)
        expires_at = s.expires_at
        item.update({
            "recent_uploads_24h": recent,
            "remaining_uploads": max(0, max_uploads - upload_count),
            "session_max_uploads": max_uploads,
            "session_upload_count": upload_count,
            "session_simple_code": s.simple_code,
            "session_expiring_soon": bool(expires_at) and _utc(expires_at) <= now + timedelta(days=7),
            "session_expires_at": expires_at.isoformat() if expires_at else None,
        })
        enriched.append(item)
    return enriched
