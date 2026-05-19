"""S3 (MinIO) helper functions."""

import logging
import threading
from io import BytesIO
from typing import Optional

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import S3Config, UPLOAD_MAX_FILE_SIZE_MB

logger = logging.getLogger(__name__)

# Cache thread-safe des clients boto3. La création d'un client boto3 est
# coûteuse (loaders de schémas + setup signing + HTTPS session, ~50-200ms),
# ce qui rendait /api/my-sessions monstrueusement lent quand on parallélise
# des dizaines de HEAD probes (chacun créait son propre client). Les
# clients boto3 sont documentés thread-safe pour les opérations de lecture
# (cf. https://boto3.amazonaws.com/v1/documentation/api/latest/guide/clients.html
# "Multithreading or multiprocessing with clients"), donc on peut partager
# une instance par (endpoint, bucket, access_key).
_CLIENT_CACHE: dict = {}
_CLIENT_CACHE_LOCK = threading.Lock()


# Pin boto3's multipart_threshold just above the pipeline's accepted file cap so
# every legitimate upload goes through a single PUT and only needs s3:PutObject.
# Rationale: boto3's default 8 MB threshold splits most of our files into
# multipart, which on a hardened S3 (MinIO with strict bucket policy, AWS S3
# with separate IAM grants for CreateMultipartUpload/UploadPart/...) would
# require a broader IAM allowlist. Coupling the threshold to
# UPLOAD_MAX_FILE_SIZE_MB means: if someone raises the upload cap above this
# threshold without revisiting IAM, multipart kicks back in and the failure is
# loud rather than silent. The +64 MB headroom absorbs re-encoding overhead
# (transcoded files can grow slightly versus the original).
_SINGLE_PUT_THRESHOLD_BYTES = (UPLOAD_MAX_FILE_SIZE_MB + 64) * 1024 * 1024
_NO_MULTIPART = TransferConfig(multipart_threshold=_SINGLE_PUT_THRESHOLD_BYTES)


def get_s3_client(cfg: S3Config):
    """Return a (cached) boto3 S3 client for the given config.

    Le pool de connexions HTTPS du botocore est lui aussi tenu par le
    client — réutiliser la même instance permet de keep-alive vers S3 et
    d'éviter le coût de TLS handshake répété. ``max_pool_connections``
    relevé pour ne pas brider les batchs parallèles (default = 10).
    """
    cache_key = (cfg.endpoint, cfg.bucket, cfg.access_key, cfg.region)
    client = _CLIENT_CACHE.get(cache_key)
    if client is not None:
        return client
    with _CLIENT_CACHE_LOCK:
        # Double-check après acquisition du lock (un autre thread peut
        # avoir créé le client pendant qu'on attendait).
        client = _CLIENT_CACHE.get(cache_key)
        if client is not None:
            return client
        client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            aws_access_key_id=cfg.access_key,
            aws_secret_access_key=cfg.secret_key,
            region_name=cfg.region,
            config=BotoConfig(
                signature_version="s3v4",
                max_pool_connections=64,
            ),
        )
        _CLIENT_CACHE[cache_key] = client
        return client


def ensure_bucket(cfg: S3Config):
    """Create the bucket if it doesn't exist."""
    client = get_s3_client(cfg)
    try:
        client.head_bucket(Bucket=cfg.bucket)
        return
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"403", "AccessDenied"}:
            # Some managed S3 policies deny HeadBucket while allowing object-level
            # operations on pre-created buckets. Don't block service startup.
            logger.warning(
                "HeadBucket denied for %s on %s (%s); skipping bucket existence check.",
                cfg.bucket,
                cfg.endpoint,
                code,
            )
            return
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise

    logger.info("Creating bucket %s", cfg.bucket)
    try:
        client.create_bucket(Bucket=cfg.bucket)
    except ClientError as exc:
        # Another worker/process may have created the bucket concurrently.
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def upload_fileobj(cfg: S3Config, key: str, data: BytesIO, content_type: str = "application/octet-stream") -> str:
    """Upload a file-like object to S3. Returns the key."""
    client = get_s3_client(cfg)
    client.upload_fileobj(
        data,
        cfg.bucket,
        key,
        ExtraArgs={"ContentType": content_type},
        Config=_NO_MULTIPART,
    )
    logger.info("Uploaded %s to %s/%s", key, cfg.bucket, key)
    return key


def download_fileobj(cfg: S3Config, key: str) -> BytesIO:
    """Download a file from S3 into a BytesIO."""
    client = get_s3_client(cfg)
    buf = BytesIO()
    client.download_fileobj(cfg.bucket, key, buf)
    buf.seek(0)
    return buf


def delete_object(cfg: S3Config, key: str):
    """Delete an object from S3."""
    client = get_s3_client(cfg)
    client.delete_object(Bucket=cfg.bucket, Key=key)


def object_exists(cfg: S3Config, key: str) -> bool:
    """Check whether an object exists in S3."""
    client = get_s3_client(cfg)
    try:
        client.head_object(Bucket=cfg.bucket, Key=key)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        if code in {"403", "AccessDenied"}:
            # Some S3 policies allow GetObject but deny HeadObject.
            # Treat as "not verifiable" and hide the direct link in UI.
            return False
        raise


def generate_presigned_url(cfg: S3Config, key: str, expires_in: int = 3600) -> str:
    """Generate a presigned download URL."""
    client = get_s3_client(cfg)
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg.bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def copy_between_buckets(
    src_cfg: S3Config, src_key: str,
    dst_cfg: S3Config, dst_key: str
) -> str:
    """Download from src and upload to dst (cross-endpoint safe)."""
    data = download_fileobj(src_cfg, src_key)
    upload_fileobj(dst_cfg, dst_key, data)
    return dst_key
