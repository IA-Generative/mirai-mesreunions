"""Configuration centralisée chargée depuis les variables d'environnement."""

import os
from dataclasses import dataclass, field
from typing import List


def _int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _list(key: str, default: str = "") -> List[str]:
    return [x.strip() for x in os.getenv(key, default).split(",") if x.strip()]


@dataclass
class OIDCConfig:
    issuer: str = field(default_factory=lambda: _str("OIDC_ISSUER"))
    client_id: str = field(default_factory=lambda: _str("OIDC_CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _str("OIDC_CLIENT_SECRET"))
    redirect_uri: str = field(default_factory=lambda: _str("OIDC_REDIRECT_URI"))


@dataclass
class DatabaseConfig:
    host: str = ""
    port: int = 5432
    name: str = ""
    user: str = ""
    password: str = ""

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_url(self) -> str:
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


@dataclass
class S3Config:
    endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    bucket: str = ""
    region: str = "us-east-1"


@dataclass
class RabbitMQConfig:
    host: str = field(default_factory=lambda: _str("RABBITMQ_HOST", "rabbitmq"))
    port: int = field(default_factory=lambda: _int("RABBITMQ_PORT", 5672))
    user: str = field(default_factory=lambda: _str("RABBITMQ_USER", "audio"))
    password: str = field(default_factory=lambda: _str("RABBITMQ_PASSWORD", "guest"))
    vhost: str = field(default_factory=lambda: _str("RABBITMQ_VHOST", "audio_pipeline"))

    @property
    def url(self) -> str:
        return f"amqp://{self.user}:{self.password}@{self.host}:{self.port}/{self.vhost}"


def load_ext_db() -> DatabaseConfig:
    return DatabaseConfig(
        host=_str("EXT_DB_HOST", "postgres-external"),
        port=_int("EXT_DB_PORT", 5432),
        name=_str("EXT_DB_NAME", "audio_upload_ext"),
        user=_str("EXT_DB_USER", "audio_ext"),
        password=_str("EXT_DB_PASSWORD", "changeme"),
    )


def load_int_db() -> DatabaseConfig:
    return DatabaseConfig(
        host=_str("INT_DB_HOST", "postgres-internal"),
        port=_int("INT_DB_PORT", 5432),
        name=_str("INT_DB_NAME", "audio_upload_int"),
        user=_str("INT_DB_USER", "audio_int"),
        password=_str("INT_DB_PASSWORD", "changeme"),
    )


def load_s3_upload() -> S3Config:
    return S3Config(
        endpoint=_str("S3_UPLOAD_ENDPOINT", "http://minio-upload:9000"),
        access_key=_str("S3_UPLOAD_ACCESS_KEY", "minioadmin"),
        secret_key=_str("S3_UPLOAD_SECRET_KEY", "minioadmin"),
        bucket=_str("S3_UPLOAD_BUCKET", "upload-staging"),
        region=_str("S3_UPLOAD_REGION", _str("AWS_REGION", "us-east-1")),
    )


def load_s3_processed() -> S3Config:
    return S3Config(
        endpoint=_str("S3_PROCESSED_ENDPOINT", "http://minio-processed:9000"),
        access_key=_str("S3_PROCESSED_ACCESS_KEY", "minioadmin"),
        secret_key=_str("S3_PROCESSED_SECRET_KEY", "minioadmin"),
        bucket=_str("S3_PROCESSED_BUCKET", "processed-staging"),
        region=_str("S3_PROCESSED_REGION", _str("AWS_REGION", "us-east-1")),
    )


def load_s3_internal() -> S3Config:
    return S3Config(
        endpoint=_str("S3_INTERNAL_ENDPOINT", "http://minio-internal:9000"),
        access_key=_str("S3_INTERNAL_ACCESS_KEY", "minioadmin"),
        secret_key=_str("S3_INTERNAL_SECRET_KEY", "minioadmin"),
        bucket=_str("S3_INTERNAL_BUCKET", "internal-storage"),
        region=_str("S3_INTERNAL_REGION", _str("AWS_REGION", "us-east-1")),
    )


# Codes
CODE_TTL_MINUTES = _int("CODE_TTL_MINUTES", 10080)
CODE_TTL_MAX_MINUTES = _int("CODE_TTL_MAX_MINUTES", 10080)
MAX_UPLOADS_PER_SESSION = _int("MAX_UPLOADS_PER_SESSION", 299)
CODE_LENGTH = _int("CODE_LENGTH", 6)
UPLOAD_STATUS_VIEW_TTL_MINUTES = _int("UPLOAD_STATUS_VIEW_TTL_MINUTES", 60)
UPLOAD_EXPIRY_GRACE_SECONDS = _int("UPLOAD_EXPIRY_GRACE_SECONDS", 300)
SECRET_KEY = _str("SECRET_KEY", "dev-secret-change-me")
UPLOAD_MAX_FILE_SIZE_MB = _int("UPLOAD_MAX_FILE_SIZE_MB", 100)
ALLOWED_AUDIO_EXTENSIONS = _list("ALLOWED_AUDIO_EXTENSIONS", "mp3,wav,ogg,flac,m4a,aac,wma,opus,webm")
UPLOAD_PORTAL_BASE_URL = _str("UPLOAD_PORTAL_BASE_URL", "http://localhost:8081")
MYDEVICES_PORTAL_URL = _str("MYDEVICES_PORTAL_URL", "")
TOKEN_EXPIRY_WARNING_DAYS = _int("TOKEN_EXPIRY_WARNING_DAYS", 7)
# Internal-pull cross-cluster channel.
#   INTERNAL_PUSH_TRIGGER_URL : if it parses as an HTTP(S) URL with a non-empty
#       host, file-mover will POST a wake-up to that URL after publishing the
#       internal_pull AMQP message; otherwise the trigger is disabled and
#       file-puller picks the message up at the next polling tick.
#   INTERNAL_PULL_QUEUE_INTERVAL_SECONDS : how often file-puller drains the
#       internal_pull queue. Acts as a safety net even when the HTTP trigger
#       is enabled — anything missed by the trigger is caught next tick.
INTERNAL_PUSH_TRIGGER_URL = _str("INTERNAL_PUSH_TRIGGER_URL", "")
INTERNAL_PULL_QUEUE_INTERVAL_SECONDS = _int("INTERNAL_PULL_QUEUE_INTERVAL_SECONDS", 30)
FFMPEG_AUDIO_FILTER = _str("FFMPEG_AUDIO_FILTER", "highpass=f=80,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11")
TRANSCODE_SAMPLE_RATE = _int("TRANSCODE_SAMPLE_RATE", 16000)
TRANSCODE_CHANNELS = _int("TRANSCODE_CHANNELS", 1)
# Auto-skip loudnorm when the input is already loud enough.
# Probes RMS (dBFS) on small windows; if max measured >= threshold, skip the
# 2-pass loudnorm (still applies highpass/lowpass/limiter post-chain).
# Bench `bench/reports/SYNTHESE.md` showed loudnorm gives 0 WER gain on
# already-loud audio, so the work is wasted ~80% of the time in practice.
LOUDNORM_AUTO_DECISION = _bool("LOUDNORM_AUTO_DECISION", True)
LOUDNORM_RMS_THRESHOLD_DBFS = float(_str("LOUDNORM_RMS_THRESHOLD_DBFS", "-30.0"))
LOUDNORM_PROBE_OFFSETS_S = _str("LOUDNORM_PROBE_OFFSETS_S", "60,300")
LOUDNORM_PROBE_DURATION_S = float(_str("LOUDNORM_PROBE_DURATION_S", "5.0"))
INTERNAL_API_URL = _str("INTERNAL_API_URL", "http://file-puller:8090/api/v1/pull")
INTERNAL_API_TOKEN = _str("INTERNAL_API_TOKEN", "")
TOKEN_ISSUER_API_URL = _str("TOKEN_ISSUER_API_URL", "http://token-issuer:8091/api/v1/issue-token")

# Transcription backend selection. Mutually exclusive — a file goes to
# exactly one destination.
#   TRANSCRIPTION_BACKEND  : "stub" (default, local transcription-stub via
#                            the AMQP transcription queue), "mcr" (push to
#                            the external MCR meeting platform), or
#                            "kevent" (call the Mirai Kevent gateway for
#                            transcription + optional diarization + optional
#                            LLM-based meeting intelligence).
TRANSCRIPTION_BACKEND = _str("TRANSCRIPTION_BACKEND", "stub")

# MCR backend (when TRANSCRIPTION_BACKEND=mcr). The user-delegated push uses
# a refresh token captured at OIDC login and exchanged on demand by file-puller.
#   MCR_GATEWAY_URL             : base URL of the MCR API gateway (no trailing /).
#   OIDC_OFFLINE_ACCESS         : when true, code-generator + admin-portal
#                                 request the offline_access scope at login
#                                 and persist the resulting refresh_token.
#   OIDC_TOKEN_ENDPOINT         : Keycloak's token endpoint, used by file-puller
#                                 to exchange a refresh_token against an
#                                 access_token at MCR push time.
#   OIDC_REFRESH_TOKEN_FERNET_KEY : Fernet key (URL-safe base64) used to
#                                 encrypt persisted refresh tokens at rest.
MCR_GATEWAY_URL = _str("MCR_GATEWAY_URL", "")
OIDC_OFFLINE_ACCESS = _bool("OIDC_OFFLINE_ACCESS", False)
OIDC_TOKEN_ENDPOINT = _str("OIDC_TOKEN_ENDPOINT", "")
OIDC_REFRESH_TOKEN_FERNET_KEY = _str("OIDC_REFRESH_TOKEN_FERNET_KEY", "")

# Kevent backend (when TRANSCRIPTION_BACKEND=kevent). The Mirai inference
# gateway exposes Whisper (transcription) and pyannote (diarization) over
# multipart sync POSTs; auth is the non-standard "apikey: Bearer <token>"
# header. Sub-toggles below let an operator activate the meeting-intelligence
# steps progressively (each consumes the previous output).
KEVENT_GATEWAY_URL = _str("KEVENT_GATEWAY_URL", "")
KEVENT_API_KEY = _str("KEVENT_API_KEY", "")
KEVENT_TRANSCRIPTION_MODEL = _str("KEVENT_TRANSCRIPTION_MODEL", "faster-whisper-large-v3-turbo")
KEVENT_DIARIZATION_MODEL = _str("KEVENT_DIARIZATION_MODEL", "pyannote-diarization")
KEVENT_DIARIZATION_ENABLED = _bool("KEVENT_DIARIZATION_ENABLED", False)
KEVENT_SPEAKER_NAMING_ENABLED = _bool("KEVENT_SPEAKER_NAMING_ENABLED", False)
KEVENT_OOB_CLEANING_ENABLED = _bool("KEVENT_OOB_CLEANING_ENABLED", False)
KEVENT_REFORMULATION_ENABLED = _bool("KEVENT_REFORMULATION_ENABLED", False)
KEVENT_MEETING_ANALYSIS_ENABLED = _bool("KEVENT_MEETING_ANALYSIS_ENABLED", False)
# Glossary correction step (LLM rewrites detected acronyms / specialised terms
# using a static general glossary loaded from `glossaire/*.md|txt|json` at
# startup). Runs between speaker_naming and oob_cleaning so downstream steps
# see the corrected sigles.
KEVENT_GLOSSARY_CORRECTION_ENABLED = _bool("KEVENT_GLOSSARY_CORRECTION_ENABLED", False)
KEVENT_GLOSSARY_DIR = _str("KEVENT_GLOSSARY_DIR", "/app/glossaire")
KEVENT_GLOSSARY_MAX_TERMS_PER_CALL = _int("KEVENT_GLOSSARY_MAX_TERMS_PER_CALL", 200)
# LLM-based filename suggestion (small model, 1 cheap call) — produces a short
# title used as the stem of user-facing download filenames. NULL = fallback to
# original filename + slot suffix.
KEVENT_FILENAME_SUGGESTION_ENABLED = _bool("KEVENT_FILENAME_SUGGESTION_ENABLED", False)
KEVENT_HTTP_TIMEOUT_SECONDS = _int("KEVENT_HTTP_TIMEOUT_SECONDS", 600)

# LiteLLM Mirai (chat backend used by the kevent meeting-intelligence steps).
# OpenAI-compatible — standard "Authorization: Bearer <token>" header.
LITELLM_BASE_URL = _str("LITELLM_BASE_URL", "")
LITELLM_API_KEY = _str("LITELLM_API_KEY", "")
LLM_MODEL_SMALL = _str("LLM_MODEL_SMALL", "chat-small")
LLM_MODEL_MEDIUM = _str("LLM_MODEL_MEDIUM", "mistral-small-24b")
LLM_MODEL_LARGE = _str("LLM_MODEL_LARGE", "gptoss-120b")
LLM_HTTP_TIMEOUT_SECONDS = _int("LLM_HTTP_TIMEOUT_SECONDS", 180)
