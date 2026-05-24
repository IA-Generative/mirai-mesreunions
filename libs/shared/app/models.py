"""
SQLAlchemy models for external and internal zones.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, DateTime, Boolean, Text, Float,
    Enum as SAEnum, ForeignKey, Index, JSON
)
from sqlalchemy.dialects.postgresql import UUID, JSONB

# Au déploiement réel (postgres), on stocke en JSONB pour requêtes et
# indexes GIN. En tests (SQLite), JSONB n'est pas reconnu ; on retombe
# sur le type générique JSON. ``with_variant`` permet de garder une seule
# déclaration de colonne tout en exposant le DDL natif à postgres.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")

# UUID variant : pg natif en prod, CHAR(36) en SQLite pour les tests.
# Utile pour les colonnes UUID introduites par la migration 011 qui doivent
# rester compilables sur SQLite (tests d'isolation). Les PK ``id`` continuent
# à utiliser UUID(as_uuid=True) direct (déjà patché dans les fixtures).
_UUID_TYPE = String(36).with_variant(UUID(as_uuid=True), "postgresql")

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

    # Corbeille (soft-delete). NULL = visible. NOT NULL = mis à la corbeille
    # le YYYY-MM-DD ; sera définitivement supprimé (DB + S3) après 30 jours
    # par le balayage opportuniste de mesreunions-web (api_my_sessions).
    trashed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    uploads = relationship("UploadedFile", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_session_expires", "expires_at"),
        Index("ix_session_status_code", "status", "simple_code"),
        # /api/my-sessions filtre (user_sub, trashed_at IS NULL) puis
        # ORDER BY created_at DESC LIMIT 20 — c'est la requête chaude de
        # la liste réunions, qui scannait la table entière sans cet index.
        Index("ix_session_user_active_created", "user_sub", "trashed_at", "created_at"),
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

    # Normalization impact (mesuré par le audio-normalizer en pass-1 et
    # post-transcode). Persisté en DB pour qu'on n'ait plus besoin de
    # redownloader la source S3 — qui est purgée après transcode — quand
    # l'utilisateur clique l'icône info sur mydevices.
    normalization_source_i = Column(Float, nullable=True)
    normalization_source_tp = Column(Float, nullable=True)
    normalization_source_lra = Column(Float, nullable=True)
    normalization_output_i = Column(Float, nullable=True)
    normalization_output_tp = Column(Float, nullable=True)
    normalization_output_lra = Column(Float, nullable=True)

    # Transfer
    transferred_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Corbeille (soft-delete). NULL = visible. Cf. UploadSession.trashed_at.
    trashed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    session = relationship("UploadSession", back_populates="uploads")

    __table_args__ = (
        Index("ix_file_status", "status"),
        Index("ix_file_session_status", "session_id", "status"),
    )


class UploadTokenOption(ExternalBase):
    """Per-token behavior flags for external zone processing."""
    __tablename__ = "upload_token_options"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    qr_token = Column(String(64), nullable=False, unique=True, index=True)
    simple_code = Column(String(10), nullable=False, index=True)
    auto_transcribe = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class PreparationGenerationJob(ExternalBase):
    """Store partagé multi-pod des jobs de génération de brief.

    Remplace l'ancien in-memory dict de generation_jobs.py qui ne
    fonctionnait qu'en single-replica. Avec 2+ pods, le polling tombait
    parfois sur un pod différent du worker → 404 "Job introuvable".

    Hébergé en postgres-external parce que mesreunions-web y a accès
    direct (creds via external-db-secret). Le job est éphémère (TTL 1h
    via gc) donc pas besoin de le mettre en postgres-internal.
    """
    __tablename__ = "preparation_generation_jobs"

    id = Column(String(64), primary_key=True)  # uuid4 hex
    user_sub = Column(String(255), nullable=False, index=True)
    phase = Column(String(40), nullable=False, default="queued",
                   comment="queued|init|test_drive|listing_docs|reading_doc|"
                           "generating_llm|persisting|extracting_glossary|done|failed")
    current_doc = Column(Text, nullable=True)
    docs_processed = Column(Integer, nullable=False, default=0)
    docs_total = Column(Integer, nullable=False, default=0)
    preparation_id = Column(String(64), nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime(timezone=True), nullable=True)


# ─── Zone Interne ───────────────────────────────────────────

class IssuedToken(InternalBase):
    """
    Token de session généré côté INTERNE (autorité de confiance).
    Le mesreunions-web (ext) demande un token via API, l'interne le génère et le stocke.
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


class IssuedTokenOption(InternalBase):
    """Per-token behavior flags stored in internal trust zone."""
    __tablename__ = "issued_token_options"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    qr_token = Column(String(64), nullable=False, unique=True, index=True)
    simple_code = Column(String(10), nullable=False, index=True)
    user_sub = Column(String(255), nullable=False, index=True)
    auto_transcribe = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class DeviceEnrollment(InternalBase):
    """Persistent browser/device enrollment bound to an issued QR token."""
    __tablename__ = "device_enrollments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(String(255), nullable=False, index=True)
    qr_token = Column(String(64), nullable=False, index=True)
    simple_code = Column(String(10), nullable=False, index=True)

    device_key = Column(String(255), nullable=False, index=True, comment="Client-generated stable key")
    device_fingerprint = Column(String(1024), nullable=True)
    fp_hash = Column(String(64), nullable=True, index=True,
                     comment="Server-side normalized fingerprint hash, used for browser↔PWA fusion")
    device_name = Column(String(255), nullable=True)
    user_agent = Column(String(1024), nullable=True)

    status = Column(String(32), nullable=False, default="pending", index=True)  # pending | active | revoked
    revoked_reason = Column(String(255), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True,
                          comment="Set on first heartbeat or upload — transitions pending → active")
    purge_at = Column(DateTime(timezone=True), nullable=True, index=True,
                      comment="Auto-purge time for status='pending' rows that never confirm")

    retention_expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_device_user_status", "user_sub", "status"),
        Index("ix_device_code_status", "simple_code", "status"),
        Index("ix_device_qr_status", "qr_token", "status"),
        Index("ix_device_qr_fphash", "qr_token", "fp_hash"),
        Index("ix_device_status_purge", "status", "purge_at"),
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

    # Origine de la ligne : upload web (mesreunions-web), mobile (PWA depot),
    # ou import depuis MCR (compte-rendu.mirai). Persisté pour différencier les
    # cas dans l'UI (badge "venu de MCR") + dédoublonnage à l'import.
    origin = Column(String(20), nullable=False, default="upload",
                    comment="upload | mobile | mcr_import")

    # Transcription — final text produced by whichever backend ran.
    transcription_status = Column(String(50), default="pending",
                                  comment=(
                                      "stub: pending|disabled|processing|completed|failed | "
                                      "mcr push: mcr_pushed|mcr_auth_failed|mcr_rejected|mcr_push_failed | "
                                      "mcr import: mcr_import_pending|mcr_imported|mcr_transcript_only|mcr_import_failed | "
                                      "kevent: kevent_transcribing|kevent_queued|"
                                      "kevent_processing|kevent_completed|"
                                      "kevent_partially_completed|kevent_failed"
                                  ))
    transcription_text = Column(Text, nullable=True)
    transcription_started_at = Column(DateTime(timezone=True), nullable=True)
    transcription_completed_at = Column(DateTime(timezone=True), nullable=True)
    transcription_engine = Column(String(50), nullable=True,
                                  comment="stub | mcr | kevent — which backend produced this row")
    transcription_language = Column(String(10), nullable=True,
                                    comment="ISO-639-1 code detected by the engine (kevent/Whisper)")
    # Kevent job_id du WHISPER en cours / dernier soumis. Persisté pour :
    # 1) demander la position d'attente au gateway via /api/queue-status?job_id=…
    # 2) reprendre automatiquement un poll orphelin au boot d'un pod
    #    internal-ingester (OOM, scale-down, rollout) sans re-uploader.
    kevent_job_id = Column(String(64), nullable=True, index=True,
                            comment="Kevent gateway job_id (Whisper) — track + resume on pod restart")

    # MCR push (when TRANSCRIPTION_BACKEND=mcr): the meeting_id returned by POST /meetings.
    # Used for cross-reference with the MCR platform when investigating outcomes.
    mcr_meeting_id = Column(String(64), nullable=True, index=True)

    # Kevent meeting-intelligence outputs (TRANSCRIPTION_BACKEND=kevent). Each
    # column is NULL when the corresponding sub-toggle is off OR when the
    # step failed (the row's status will be kevent_partially_completed in
    # the latter case).
    diarization_json = Column(Text, nullable=True,
                              comment="raw pyannote segments: [{speaker, start, end}, …]")
    # Word-level timestamps issus de Whisper word_timestamps=true (migration
    # 017). Format compact: [{"w": "...", "s": <sec>, "e": <sec>}, ...].
    # NULL si non-émis (fallback frontend = highlight par bloc).
    transcription_words_json = Column(Text, nullable=True,
                                      comment='Whisper word-level timestamps. NULL si word_timestamps absent. Format: [{"w","s","e"}]')
    speaker_tagged_text = Column(Text, nullable=True,
                                 comment="markdown with Intervenant_NN labels (real names if naming enabled); legacy rows may still contain SPEAKER_NN")
    glossary_corrected_text = Column(Text, nullable=True,
                                     comment="speaker_tagged_text with general glossary terms (sigles MI etc.) corrected by LLM; NULL if KEVENT_GLOSSARY_CORRECTION_ENABLED off or no relevant terms found")
    cleaned_text = Column(Text, nullable=True,
                          comment="LLM-cleaned version with out-of-band content filtered out")
    reformulated_text = Column(Text, nullable=True,
                               comment="indirect-speech reformulation: 'X a dit que…, Y a répondu…'")
    meeting_analysis_json = Column(Text, nullable=True,
                                   comment="5-section structured analysis: actors/themes/decisions/gaps/recommendations")
    suggested_filename = Column(String(255), nullable=True,
                                comment="LLM-suggested short title (4-8 words), used by user-facing downloads as filename stem; NULL if KEVENT_FILENAME_SUGGESTION_ENABLED off or step failed")
    # Date/heure *réelle* de la réunion telle que renseignée par l'utilisateur
    # depuis la fiche détaillée mydevices. NULL = pas d'override (l'UI retombe
    # sur created_at pour l'affichage et le tri). Persisté en zone interne car
    # info utilisateur durable, cohérent avec suggested_filename.
    meeting_datetime = Column(DateTime(timezone=True), nullable=True,
                              comment="User-specified actual meeting date/time override")
    key_points_summary = Column(Text, nullable=True,
                                comment="LLM-generated 3-5 bullet summary of the meeting key points (one short sentence each); displayed as subtitle in mydevices UI; NULL if step disabled or failed")
    absentee_summary = Column(Text, nullable=True,
                              comment="self-contained 150-300 words debrief written for people who missed the meeting; NULL if KEVENT_ABSENTEE_SUMMARY_ENABLED off or step failed")

    pulled_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # ─── Liaison meeting ↔ audio (migration 012) ────────────────────
    # Lien explicite vers la Meeting (post-réunion) dont cet audio est la
    # source. NULL = audio non encore rattaché à un meeting. ON DELETE SET
    # NULL : si le meeting est hard-deleted, on conserve l'audio.
    # Migration 012 : ancien meeting_brief_id renommé en meeting_id (FK vers
    # meetings au lieu de meeting_briefs).
    meeting_id = Column(_UUID_TYPE, nullable=True, index=True)
    # Tracking du re-traitement post-link avec glossaire amendé. Cf §5.
    reprocess_version = Column(Integer, nullable=False, default=0)
    reprocessed_with_meeting_id = Column(_UUID_TYPE, nullable=True)
    last_reprocessed_at = Column(DateTime(timezone=True), nullable=True)
    reprocess_history = Column(_JSON_TYPE, nullable=False, default=list)
    suggested_meeting_dismissed_id = Column(_UUID_TYPE, nullable=True)

    # Alias legacy supprimés en PR2d. Les noms canoniques migration 012 sont
    # `meeting_id`, `reprocessed_with_meeting_id`, `suggested_meeting_dismissed_id`.

    # ─── Pipeline watchdog (migration 017) ───────────────────────────
    # Heartbeat applicatif : touché à chaque étape clé du pipeline
    # (submit Kevent, poll status, application résultat). Permet au
    # watchdog de détecter les jobs orphelins (last_activity_at trop
    # vieux pour un status non-terminal). Default = created_at.
    last_activity_at = Column(DateTime(timezone=True), nullable=True)
    # Lease court (90s par défaut) pour éviter qu'un job orphelin soit
    # repris par plusieurs pods simultanément. Le watchdog claim via
    # UPDATE atomique avec WHERE (pipeline_claim_at IS NULL OR < NOW()-90s).
    pipeline_claim_at = Column(DateTime(timezone=True), nullable=True)
    pipeline_claim_pod = Column(String(128), nullable=True)

    # ─── Observabilité erreur (migration 020) ────────────────────────
    # Peuplé par tous les call-sites passant en *_failed (mcr_importer,
    # pipeline_watchdog._mark_capped_as_failed, puller Kevent path).
    # Permet de débugger depuis la DB et de surfacer un message UI clair.
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    last_error_kind = Column(String(64), nullable=True)
    last_error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_user_audio_user", "user_sub"),
        Index("ix_user_audio_transcription", "transcription_status"),
        # Index utilisé par le watchdog : balaie tous les jobs non-terminaux
        # dont last_activity_at est trop vieux. Ordre des colonnes choisi
        # pour que postgres puisse short-circuiter sur transcription_status
        # avant le scan temporel.
        Index("ix_user_audio_watchdog",
              "transcription_status", "last_activity_at"),
    )


class OidcRefreshToken(InternalBase):
    """
    Server-side cache of an OIDC refresh token, keyed by user_sub.

    Captured at login on mydevices (mesreunions-web/admin-console) when the
    OIDC scope ``offline_access`` is requested. Used asynchronously by
    internal-ingester at MCR push time to mint a fresh access token *on behalf
    of* the original user — without that user being interactively
    connected anymore.

    Stored as Fernet ciphertext (cf libs.shared.app.secrets_crypto). The
    plaintext refresh token never touches the disk in clear.

    Lifecycle:
      - INSERT/UPSERT at user login (latest token wins; Keycloak rotates
        on use so older tokens become invalid anyway).
      - SELECT-DECRYPT-EXCHANGE at internal-ingester MCR push.
      - DELETE on KC ``invalid_grant`` (refresh expired or revoked) or on
        explicit logout.
    """
    __tablename__ = "oidc_refresh_tokens"

    user_sub = Column(String(255), primary_key=True)
    ciphertext = Column(Text, nullable=False, comment="Fernet ciphertext of the refresh_token")
    keycloak_iss = Column(String(512), nullable=True,
                          comment="OIDC issuer at the time of capture, for multi-realm safety")
    user_email = Column(String(255), nullable=True,
                        comment="Convenience copy from id_token, for ops/debug only — ground truth is user_sub")
    last_login_at = Column(DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Preparation(InternalBase):
    """Préparation amont-réunion (migration 012 — ex ``meeting_briefs``).

    Entité de première classe portant la préparation utilisateur :
    titre/contexte/participants/contenu LLM/glossaire-source/sync Drive.
    Le pendant post-réunion est ``Meeting`` (CR). Une préparation peut
    exister sans meeting (réunion non encore tenue) et un meeting peut
    exister sans préparation (CR manuel) — cardinalité 0..1 ↔ 0..1.

    Isolation par ``user_sub`` (OIDC), soft-delete via ``trashed_at``,
    purge auto 30j déclenchée par mesreunions-web.

    L'écriture/lecture depuis mesreunions-web (zone externe) passe par
    device-token-authority ``/api/v1/preparations/*`` (relais cross-cluster).
    """
    __tablename__ = "preparations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(String(255), nullable=False, index=True,
                      comment="OIDC subject identifier — owner of the preparation")

    # Contenu utilisateur (saisie wizard).
    title = Column(Text, nullable=True)
    subject = Column(Text, nullable=True)
    role = Column(Text, nullable=True)
    expectation = Column(Text, nullable=True)
    focus = Column(_JSON_TYPE, nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    participants = Column(_JSON_TYPE, nullable=True)
    context = Column(Text, nullable=True)
    target_meeting_date = Column(DateTime(timezone=True), nullable=True,
                                 comment="Date prévue de la réunion (saisie wizard)")

    # Sortie LLM (brief structuré) + métadonnées documents ingérés.
    content = Column(_JSON_TYPE, nullable=True,
                     comment="ex brief_json — sortie LLM structurée")
    documents = Column(_JSON_TYPE, nullable=True)
    glossary_source = Column(_JSON_TYPE, nullable=True,
                             comment="termes glossaire extraits, source du user_glossary_terms")

    # Chaînage série (préparation parent dans une chaîne de réunions).
    series_parent_id = Column(_UUID_TYPE, nullable=True)

    # Sync Drive best-effort (cf §9bis du plan v1).
    drive_folder_id = Column(Text, nullable=True)
    drive_prep_folder_id = Column(Text, nullable=True)
    drive_prep_root_folder_id = Column(Text, nullable=True)
    drive_sync_status = Column(Text, nullable=True,
                               comment="'pending' | 'synced' | 'failed'")
    drive_synced_at = Column(DateTime(timezone=True), nullable=True)

    # Engagement (signal d'auto-link avec un audio).
    last_viewed_at = Column(DateTime(timezone=True), nullable=True)

    # ─── Récurrence (Lot 6 — migration 013) ─────────────────────────
    # Réunion récurrente : flag de filtre rapide + règle structurée
    # (RRULE-like) + prochaine occurrence pré-calculée backend (via
    # python-dateutil) pour permettre le tri/affichage sans recalcul.
    is_recurring = Column(Boolean, nullable=True, default=False)
    recurrence_rule = Column(_JSON_TYPE, nullable=True,
                             comment="{freq, interval, byweekday, byhour, byminute, until}")
    next_occurrence_at = Column(DateTime(timezone=True), nullable=True)

    # ─── Thématiques personnalisables (Lot 9 — migration 014) ───────
    # Liste libre de chips utilisateur en complément du champ `focus`
    # (checkboxes pré-définies). Capée 50 côté backend.
    themes = Column(_JSON_TYPE, nullable=True,
                    comment="liste libre de thématiques utilisateur (cap 50)")

    # ─── Email CR auto (Lot 8 — migration 014) ──────────────────────
    # Toggle : envoi auto du CR aux participants[].email à la fin de la
    # transcription (consommé par le hook côté pipeline ingester via
    # POST /api/meetings/{id}/send-cr).
    send_cr_email = Column(Boolean, nullable=True, default=False)

    # ─── Main courante Drive (Lot 8 — migration 014) ────────────────
    # Pour les réunions récurrentes : doc Drive cumulatif qui regroupe
    # tous les CR de la série. Créé à la première occurrence avec CR.
    drive_main_courante_doc_id = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=True,
                        onupdate=lambda: datetime.now(timezone.utc))
    # Corbeille (soft-delete). NULL = visible.
    trashed_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Les index partiels sont créés par migration 012 (non re-déclarés ici
    # pour éviter qu'init_tables crée des versions non-partielles).


class Meeting(InternalBase):
    """Réunion / CR post-réunion (migration 012 — split de ``meeting_briefs``).

    Entité de première classe portant le rendu utilisateur post-réunion :
    titre/résumé/CR structuré. Liée optionnellement à un audio (source) et
    à une préparation (amont). FK NULLABLES : un meeting peut exister
    standalone (CR manuel sans audio, sans prep préalable).

    Isolation par ``user_sub`` (OIDC), soft-delete via ``trashed_at``.
    L'écriture/lecture depuis mesreunions-web (zone externe) passe par
    device-token-authority ``/api/v1/meetings/*``.
    """
    __tablename__ = "meetings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(String(255), nullable=False, index=True,
                      comment="OIDC subject identifier — owner of the meeting")

    # Identité côté utilisateur.
    title = Column(Text, nullable=True)
    summary = Column(Text, nullable=True,
                     comment="résumé court (key_points)")
    content = Column(_JSON_TYPE, nullable=True,
                     comment="compte-rendu structuré complet")

    # Lien optionnel vers l'audio source (NULL si CR manuel).
    user_audio_file_id = Column(_UUID_TYPE, nullable=True, index=True)
    # Lien optionnel vers la préparation amont (NULL si pas de prep).
    preparation_id = Column(_UUID_TYPE, nullable=True, index=True)

    # Sync Drive best-effort.
    drive_folder_id = Column(Text, nullable=True)
    drive_sync_status = Column(Text, nullable=True,
                               comment="'pending' | 'synced' | 'failed'")
    drive_synced_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False,
                        default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=True,
                        onupdate=lambda: datetime.now(timezone.utc))
    # Corbeille (soft-delete).
    trashed_at = Column(DateTime(timezone=True), nullable=True, index=True)


class UserGlossaryTerm(InternalBase):
    """Glossaire utilisateur global (§5c du plan).

    Accumule au fil de l'eau les termes (sigles, noms propres, jargon métier)
    extraits des briefs d'un même ``user_sub``. Alimente glossary_correction
    pour TOUTES les transcriptions de cet utilisateur, y compris audios sans
    brief lié. Composite PK ``(user_sub, term)`` pour UPSERT idempotent.

    Cap 300 termes au chargement (cf glossary_loader). Cap 200 par appel LLM
    via filter_relevant() existant.
    """
    __tablename__ = "user_glossary_terms"

    user_sub = Column(String(255), primary_key=True)
    term = Column(String(255), primary_key=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
    last_seen_at = Column(DateTime(timezone=True), nullable=False,
                          default=lambda: datetime.now(timezone.utc))
    occurrence_count = Column(Integer, nullable=False, default=1)
    # Migration 012 : ancien last_source_brief_id renommé en
    # last_source_meeting_id (la traçabilité par brief a été remplacée par
    # la traçabilité par meeting/preparation).
    last_source_meeting_id = Column(_UUID_TYPE, nullable=True)
    # Termes ajoutés/validés manuellement par l'utilisateur (UI curation).
    curated_by_user = Column(Boolean, nullable=False, default=False)
    # Termes rejetés explicitement, ne plus re-proposer ni utiliser.
    blacklisted = Column(Boolean, nullable=False, default=False)

    # Alias legacy `last_source_brief_id` supprimé en PR2d. Utiliser
    # `last_source_meeting_id` (nom canonique migration 012).


# Alias `MeetingBrief = Preparation` et property `Preparation.brief_json`
# supprimés en PR2d. Importer `Preparation` directement et utiliser
# `Preparation.content` (renommé en migration 012).


class UserFeedback(InternalBase):
    """Feedback utilisateur — pouce ↑/↓ ou demande de regénération.

    Migration 015. Centralise 2 types de feedback laissés depuis la fiche
    détail d'une réunion :

      • ``type='usefulness'`` → pouce ↑/↓ + checklist raisons + free-text
        payload : {thumb: 'up'|'down', reasons: [...], free_text: str}
      • ``type='regenerate'`` → demande de relancer un pipeline
        payload : {scope: 'full'|'llm-only', reason: str}

    Cycle de vie via ``status`` : ``new`` (créé) → ``processed`` (admin a
    pris en compte) ou ``dismissed`` (écarté). L'utilisateur voit ses
    feedbacks dans "Mes données utiles" avec un tag "pris en compte" si
    ``status='processed'``.
    """
    __tablename__ = "user_feedback"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_sub = Column(Text, nullable=False)
    # file_id peut être NULL (feedback global) — pas de FK pour ne pas
    # perdre l'historique quand un fichier est trashé.
    file_id = Column(UUID(as_uuid=True), nullable=True)
    type = Column(String(32), nullable=False)
    payload = Column(_JSON_TYPE, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="new")
    ai_suggestion = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    processed_by = Column(Text, nullable=True)
    admin_comment = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_user_feedback_user_created", "user_sub", "created_at"),
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
