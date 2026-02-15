"""S3 (MinIO) helper functions."""

import logging
from io import BytesIO
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import S3Config

logger = logging.getLogger(__name__)


def get_s3_client(cfg: S3Config):
    """Create a boto3 S3 client for the given config."""
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        aws_access_key_id=cfg.access_key,
        aws_secret_access_key=cfg.secret_key,
        region_name=cfg.region,
        config=BotoConfig(signature_version="s3v4"),
    )


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
