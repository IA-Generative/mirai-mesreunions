"""Configuration centralisée chargée depuis les variables d'environnement."""

import os
from dataclasses import dataclass, field
from typing import List


def _int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


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
        region=_str("S3_UPLOAD_REGION", "us-east-1"),
    )


def load_s3_processed() -> S3Config:
    return S3Config(
        endpoint=_str("S3_PROCESSED_ENDPOINT", "http://minio-processed:9000"),
        access_key=_str("S3_PROCESSED_ACCESS_KEY", "minioadmin"),
        secret_key=_str("S3_PROCESSED_SECRET_KEY", "minioadmin"),
        bucket=_str("S3_PROCESSED_BUCKET", "processed-staging"),
        region=_str("S3_PROCESSED_REGION", "us-east-1"),
    )


def load_s3_internal() -> S3Config:
    return S3Config(
        endpoint=_str("S3_INTERNAL_ENDPOINT", "http://minio-internal:9000"),
        access_key=_str("S3_INTERNAL_ACCESS_KEY", "minioadmin"),
        secret_key=_str("S3_INTERNAL_SECRET_KEY", "minioadmin"),
        bucket=_str("S3_INTERNAL_BUCKET", "internal-storage"),
        region=_str("S3_INTERNAL_REGION", "us-east-1"),
    )


# Codes
CODE_TTL_MINUTES = _int("CODE_TTL_MINUTES", 15)
CODE_TTL_MAX_MINUTES = _int("CODE_TTL_MAX_MINUTES", 4320)
MAX_UPLOADS_PER_SESSION = _int("MAX_UPLOADS_PER_SESSION", 5)
CODE_LENGTH = _int("CODE_LENGTH", 6)
UPLOAD_STATUS_VIEW_TTL_MINUTES = _int("UPLOAD_STATUS_VIEW_TTL_MINUTES", 60)
UPLOAD_EXPIRY_GRACE_SECONDS = _int("UPLOAD_EXPIRY_GRACE_SECONDS", 300)
SECRET_KEY = _str("SECRET_KEY", "dev-secret-change-me")
UPLOAD_MAX_FILE_SIZE_MB = _int("UPLOAD_MAX_FILE_SIZE_MB", 100)
ALLOWED_AUDIO_EXTENSIONS = _list("ALLOWED_AUDIO_EXTENSIONS", "mp3,wav,ogg,flac,m4a,aac,wma,opus,webm")
UPLOAD_PORTAL_BASE_URL = _str("UPLOAD_PORTAL_BASE_URL", "http://localhost:8081")
FFMPEG_AUDIO_FILTER = _str("FFMPEG_AUDIO_FILTER", "highpass=f=80,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11")
TRANSCODE_SAMPLE_RATE = _int("TRANSCODE_SAMPLE_RATE", 16000)
TRANSCODE_CHANNELS = _int("TRANSCODE_CHANNELS", 1)
INTERNAL_API_URL = _str("INTERNAL_API_URL", "http://file-puller:8090/api/v1/pull")
INTERNAL_API_TOKEN = _str("INTERNAL_API_TOKEN", "")
TOKEN_ISSUER_API_URL = _str("TOKEN_ISSUER_API_URL", "http://token-issuer:8091/api/v1/issue-token")
