"""
Helpers d'upload partagés entre mobile-upload-pwa (zone externe, flow QR/PWA)
et mydevices-web (zone externe, flow upload local OIDC).

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
