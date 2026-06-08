"""
Helpers d'upload partagés entre mobile-upload-pwa (zone externe, flow QR/PWA)
et mesreunions-web (zone externe, flow upload local OIDC).

Concentre la mécanique commune : validation extension, génération du nom S3,
PUT S3, publication du message AV scan. La logique propre à chaque endpoint
(auth, lookup/création de session, réponse spécifique) reste dans chaque
service.
"""

from __future__ import annotations

from io import BytesIO
from uuid import uuid4

from werkzeug.utils import secure_filename

from .config import ALLOWED_AUDIO_EXTENSIONS, RabbitMQConfig, S3Config
from .queue_helper import publish_message, QUEUE_AV_SCAN
from .s3_helper import upload_fileobj


def is_allowed_audio_filename(filename: str) -> bool:
    """True si l'extension fait partie de ALLOWED_AUDIO_EXTENSIONS."""
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_AUDIO_EXTENSIONS


def sniff_audio_magic(data: bytes) -> str | None:
    """Détecte le conteneur audio par ses octets de tête (magic bytes).

    Renvoie un label de famille (``mp3``, ``wav``, ``ogg``, ``flac``,
    ``mp4`` (m4a/aac), ``asf`` (wma), ``ebml`` (webm), ``aac``, ``amr``) ou
    ``None`` si rien de reconnu. Complète la validation par extension : un
    contenu non-audio sous une extension audio est ainsi rejeté à la porte.
    """
    if not data or len(data) < 4:
        return None
    head = data[:16]

    if head[:3] == b"ID3":
        return "mp3"
    # MP3 / AAC-ADTS : frame sync 0xFFE.. / 0xFFF..
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "aac" if (head[1] & 0xF6) in (0xF0, 0xF1, 0xF8, 0xF9) else "mp3"
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"  # ogg vorbis / opus
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "mp4"  # m4a / aac dans conteneur mp4
    if head[:4] == b"\x1aE\xdf\xa3":
        return "ebml"  # webm / matroska
    if head[:4] == b"\x30\x26\xb2\x75":
        return "asf"  # wma (ASF GUID)
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    return None


def looks_like_audio(data: bytes) -> bool:
    """True si les octets de tête correspondent à un conteneur audio connu."""
    return sniff_audio_magic(data) is not None


def magic_bytes_enforced() -> bool:
    """Mode d'application du contrôle magic bytes.

    ``UPLOAD_MAGIC_BYTES_MODE`` = ``enforce`` (défaut) rejette les contenus
    non reconnus ; ``log`` se contente de tracer (déploiement progressif).
    """
    import os
    return (os.getenv("UPLOAD_MAGIC_BYTES_MODE", "enforce") or "enforce").strip().lower() != "log"


def build_stored_filename(simple_code: str, original_filename: str) -> str:
    """Construit le nom S3 : ``{simple_code}_{uuid8}_{sanitized}``.

    Format historique partagé par tous les workers du pipeline (av-scanner,
    audio-normalizer, dmz-to-internal-bridge). Le préfixe simple_code permet d'isoler
    par session lors des debug ; l'UUID prévient les collisions si le même
    nom est uploadé deux fois ; le sanitize neutralise les chars dangereux
    (slashes, espaces multiples, etc.).
    """
    safe = secure_filename(original_filename) or "audio.bin"
    return f"{simple_code}_{uuid4().hex[:8]}_{safe}"


def store_audio_to_s3(
    s3_cfg: S3Config,
    stored_filename: str,
    file_data: bytes,
    content_type: str | None = None,
) -> None:
    """PUT du blob audio dans le bucket d'upload (zone externe).

    Le caller gère le commit DB + publish AMQP — ce helper ne fait que
    le transport S3, pour rester focalisé et facilement testable.
    """
    upload_fileobj(
        s3_cfg,
        stored_filename,
        BytesIO(file_data),
        content_type=content_type or "application/octet-stream",
    )


def publish_av_scan_message(
    rabbit_cfg: RabbitMQConfig,
    *,
    file_id: str,
    session_id: str,
    stored_filename: str,
    original_filename: str,
    simple_code: str,
    user_sub: str | None,
    user_email: str | None,
) -> None:
    """Enqueue le message AV scan vers le worker antivirus.

    Format strictement compatible avec ce qu'av-scanner consomme depuis
    QUEUE_AV_SCAN — toute modif ici impose une migration côté worker.
    """
    publish_message(rabbit_cfg, QUEUE_AV_SCAN, {
        "file_id": file_id,
        "session_id": session_id,
        "stored_filename": stored_filename,
        "original_filename": original_filename,
        "simple_code": simple_code,
        "user_sub": user_sub,
        "user_email": user_email,
    })
