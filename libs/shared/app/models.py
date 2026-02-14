"""
SQLAlchemy models for external and internal zones.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, DateTime, Boolean, Text, Float,
    Enum as SAEnum, ForeignKey, Index
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship
import enum

ExternalBase = declarative_base()
InternalBase = declarative_base()


# ─── Enums ──────────────────────────────────────────────────

class UploadStatus(str, enum.Enum):
    PENDING = "pending"
    SCANNING = "scanning"
    SCAN_CLEAN = "scan_clean"
    SCAN_INFECTED = "scan_infected"
    TRANSCODING = "transcoding"
    TRANSCODED = "transcoded"
    TRANSCODE_FAILED = "transcode_failed"
    READY_FOR_TRANSFER = "ready_for_transfer"
    TRANSFERRING = "transferring"
    TRANSFERRED = "transferred"
    QUARANTINED = "quarantined"
    ERROR = "error"


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    COMPLETED = "completed"


# ─── Zone Externe ───────────────────────────────────────────

class UploadSession(ExternalBase):
    """Session d'upload liée à un QR code / code simple."""
    __tablename__ = "upload_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(String(255), nullable=False, index=True, comment="OIDC subject identifier")
    user_email = Column(String(255), nullable=True)
    user_display_name = Column(String(255), nullable=True)

    simple_code = Column(String(10), nullable=False, unique=True, index=True)
    qr_token = Column(String(64), nullable=False, unique=True, index=True)

    status = Column(SAEnum(SessionStatus), default=SessionStatus.ACTIVE, nullable=False)
    max_uploads = Column(Integer, default=5, nullable=False)
    upload_count = Column(Integer, default=0, nullable=False)

    ttl_minutes = Column(Integer, default=15, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    status_view_expires_at = Column(DateTime(timezone=True), nullable=True,
                                     comment="Délai après lequel le statut n'est plus consultable")

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    uploads = relationship("UploadedFile", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_session_expires", "expires_at"),
        Index("ix_session_status_code", "status", "simple_code"),
    )


class UploadedFile(ExternalBase):
    """Fichier audio uploadé, avec suivi du pipeline de traitement."""
    __tablename__ = "uploaded_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), ForeignKey("upload_sessions.id"), nullable=False, index=True)

    original_filename = Column(String(512), nullable=False)
    stored_filename = Column(String(512), nullable=False, comment="Nom dans S3, inclut le code simple")
    file_size_bytes = Column(Integer, nullable=False)
    mime_type = Column(String(100), nullable=True)

    status = Column(SAEnum(UploadStatus), default=UploadStatus.PENDING, nullable=False)
    status_message = Column(Text, nullable=True)

    # Antivirus
    av_scanned_at = Column(DateTime(timezone=True), nullable=True)
    av_result = Column(String(255), nullable=True)

    # Transcodage
    transcoded_filename = Column(String(512), nullable=True)
    audio_quality_score = Column(Float, nullable=True, comment="Score qualité 1-5")
    audio_duration_seconds = Column(Float, nullable=True)
    audio_sample_rate = Column(Integer, nullable=True)

    # Transfer
    transferred_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    session = relationship("UploadSession", back_populates="uploads")

    __table_args__ = (
        Index("ix_file_status", "status"),
        Index("ix_file_session_status", "session_id", "status"),
    )


# ─── Zone Interne ───────────────────────────────────────────

class IssuedToken(InternalBase):
    """
    Token de session généré côté INTERNE (autorité de confiance).
    Le code-generator (ext) demande un token via API, l'interne le génère et le stocke.
    C'est la source de vérité pour le matching fichier ↔ utilisateur.
    """
    __tablename__ = "issued_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(String(255), nullable=False, index=True, comment="OIDC subject identifier")
    user_email = Column(String(255), nullable=True)
    user_display_name = Column(String(255), nullable=True)

    simple_code = Column(String(10), nullable=False, unique=True, index=True)
    qr_token = Column(String(64), nullable=False, unique=True, index=True)

    max_uploads = Column(Integer, default=5, nullable=False)
    ttl_minutes = Column(Integer, default=15, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    status_view_expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_token_expires", "expires_at"),
        Index("ix_token_code", "simple_code"),
    )


class UserAudioFile(InternalBase):
    """Fichier audio intégré dans le compte utilisateur (zone interne)."""
    __tablename__ = "user_audio_files"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255), nullable=True)

    original_session_code = Column(String(10), nullable=False, comment="Code simple d'origine")
    original_filename = Column(String(512), nullable=False)
    stored_filename = Column(String(512), nullable=False)

    file_size_bytes = Column(Integer, nullable=False)
    audio_quality_score = Column(Float, nullable=True)
    audio_duration_seconds = Column(Float, nullable=True)

    # Transcription
    transcription_status = Column(String(50), default="pending")
    transcription_text = Column(Text, nullable=True)
    transcription_started_at = Column(DateTime(timezone=True), nullable=True)
    transcription_completed_at = Column(DateTime(timezone=True), nullable=True)

    pulled_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_user_audio_user", "user_sub"),
        Index("ix_user_audio_transcription", "transcription_status"),
    )


class TranscriptionEvent(InternalBase):
    """Audit trail of transcription stub calls and outcomes."""
    __tablename__ = "transcription_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audio_file_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    original_session_code = Column(String(10), nullable=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    message = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index("ix_transcription_event_file_created", "audio_file_id", "created_at"),
        Index("ix_transcription_event_code_created", "original_session_code", "created_at"),
    )
