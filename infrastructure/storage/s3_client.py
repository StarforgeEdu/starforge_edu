"""S3-compatible client.

Django uses django-storages (configured in settings.STORAGES) for
default file fields. This module exposes a thin boto3 client for
direct operations: presigned URLs, multipart uploads, bucket admin.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from django.conf import settings


def _storage_options() -> dict[str, Any]:
    return settings.STORAGES["default"]["OPTIONS"]  # type: ignore[index]


@lru_cache(maxsize=1)
def get_s3_client():
    opts = _storage_options()
    return boto3.client(
        "s3",
        endpoint_url=opts.get("endpoint_url") or None,
        aws_access_key_id=opts["access_key"],
        aws_secret_access_key=opts["secret_key"],
        region_name=opts["region_name"],
        config=Config(
            signature_version=opts["signature_version"],
            s3={"addressing_style": opts["addressing_style"]},
        ),
    )


def presign_upload(key: str, *, expires_in: int = 600, content_type: str = "application/octet-stream") -> str:
    return get_s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": _storage_options()["bucket_name"],
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def presign_download(key: str, *, expires_in: int = 600) -> str:
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": _storage_options()["bucket_name"],
            "Key": key,
        },
        ExpiresIn=expires_in,
    )


def upload_bytes(key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
    """Server-side upload of an in-memory blob (e.g. a rendered PDF). Returns the
    key. Used by background tasks — never call from a request handler (DoD #9)."""
    get_s3_client().put_object(
        Bucket=_storage_options()["bucket_name"],
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


def head_object(key: str) -> dict[str, Any]:
    """Object metadata (ContentLength, ContentType, ...). Server-side — tasks only."""
    return get_s3_client().head_object(Bucket=_storage_options()["bucket_name"], Key=key)


def get_object_range(key: str, *, start: int = 0, end: int = 8191) -> bytes:
    """Fetch a byte range (inclusive) — used to sniff the first KBs for libmagic."""
    resp = get_s3_client().get_object(
        Bucket=_storage_options()["bucket_name"], Key=key, Range=f"bytes={start}-{end}"
    )
    return resp["Body"].read()


def download_bytes(key: str) -> bytes:
    """Fetch a whole object's bytes (e.g. an image to thumbnail). Tasks only."""
    resp = get_s3_client().get_object(Bucket=_storage_options()["bucket_name"], Key=key)
    return resp["Body"].read()


def copy_object(*, src_key: str, dest_key: str) -> str:
    bucket = _storage_options()["bucket_name"]
    get_s3_client().copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": src_key}, Key=dest_key)
    return dest_key


def delete_object(key: str) -> None:
    get_s3_client().delete_object(Bucket=_storage_options()["bucket_name"], Key=key)
