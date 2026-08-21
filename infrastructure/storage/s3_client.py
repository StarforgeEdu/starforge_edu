"""S3-compatible client.

Django uses django-storages (configured in settings.STORAGES) for
default file fields. This module exposes a thin boto3 client for
direct operations: presigned URLs, multipart uploads, bucket admin.
"""

from __future__ import annotations

from functools import lru_cache
from os import PathLike
from typing import Any
from urllib.parse import quote

import boto3
from botocore.client import Config
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class StorageObjectTooLarge(ValueError):
    """Raised before an object can be materialized beyond a caller's bound."""


class StorageObjectSizeMismatch(ValueError):
    """Raised when an immutable object-size contract no longer matches storage."""


def _storage_options() -> dict[str, Any]:
    return settings.STORAGES["default"]["OPTIONS"]  # type: ignore[index]


def _build_s3_client(endpoint_url: str | None):
    opts = _storage_options()
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=opts["access_key"],
        aws_secret_access_key=opts["secret_key"],
        region_name=opts["region_name"],
        config=Config(
            signature_version=opts["signature_version"],
            s3={"addressing_style": opts["addressing_style"]},
        ),
    )


@lru_cache(maxsize=1)
def get_s3_client():
    """Return the private, server-side S3 client used for object I/O."""
    return _build_s3_client(_storage_options().get("endpoint_url") or None)


@lru_cache(maxsize=1)
def get_s3_presign_client():
    """Return a client that signs browser-reachable URLs without doing I/O."""
    public_endpoint = getattr(settings, "AWS_S3_PUBLIC_ENDPOINT_URL", "").strip()
    if not public_endpoint:
        raise ImproperlyConfigured(
            "AWS_S3_PUBLIC_ENDPOINT_URL is required to generate browser-facing storage URLs."
        )
    return _build_s3_client(public_endpoint)


def presign_upload(
    key: str,
    *,
    expires_in: int = 600,
    content_type: str = "application/octet-stream",
    size_bytes: int | None = None,
) -> str:
    params: dict[str, Any] = {
        "Bucket": _storage_options()["bucket_name"],
        "Key": key,
        "ContentType": content_type,
    }
    if size_bytes is not None:
        if size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        # ContentLength becomes part of the SigV4 signed request. The browser's
        # PUT must therefore carry the exact declared length.
        params["ContentLength"] = size_bytes
    return get_s3_presign_client().generate_presigned_url(
        "put_object",
        Params=params,
        ExpiresIn=expires_in,
    )


def presign_post_upload(
    key: str,
    *,
    size_bytes: int,
    expires_in: int = 600,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """Presign a POST whose policy enforces type and exact content length.

    Unlike a presigned PUT, an S3 POST policy can carry a
    ``content-length-range`` condition which MinIO/S3 verifies before storing the
    body.  The returned ``url`` and ``fields`` are submitted as multipart form
    data by the client.
    """

    return get_s3_presign_client().generate_presigned_post(
        Bucket=_storage_options()["bucket_name"],
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", size_bytes, size_bytes],
        ],
        ExpiresIn=expires_in,
    )


def presign_download(
    key: str,
    *,
    expires_in: int = 600,
    download_filename: str | None = None,
    response_content_type: str | None = None,
) -> str:
    params: dict[str, Any] = {
        "Bucket": _storage_options()["bucket_name"],
        "Key": key,
    }
    if download_filename is not None:
        # RFC 5987 encoding avoids interpolating user-controlled quotes or
        # control characters into Content-Disposition. Domain key parsers still
        # validate the filename before this helper is called.
        encoded_name = quote(download_filename, safe="")
        params["ResponseContentDisposition"] = f"attachment; filename*=UTF-8''{encoded_name}"
    if response_content_type is not None:
        normalized_type = response_content_type.partition(";")[0].strip().lower()
        if not normalized_type or "/" not in normalized_type or len(normalized_type) > 127:
            raise ValueError("Invalid response content type")
        params["ResponseContentType"] = normalized_type
    return get_s3_presign_client().generate_presigned_url(
        "get_object",
        Params=params,
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
    """Fetch one bounded byte range (inclusive).

    Object storage is an external trust boundary.  Do not assume a compatible
    service, proxy, or test double honoured the ``Range`` header: cap the body
    read at the requested span and reject an oversized response before callers
    hand it to a parser such as libmagic.
    """
    if isinstance(start, bool) or isinstance(end, bool) or start < 0 or end < start:
        raise ValueError("Invalid object byte range")
    length = end - start + 1
    if length > 64 * 1024:
        raise ValueError("Object byte range exceeds the 64 KiB safety limit")
    resp = get_s3_client().get_object(
        Bucket=_storage_options()["bucket_name"], Key=key, Range=f"bytes={start}-{end}"
    )
    body = resp["Body"]
    try:
        data = body.read(length + 1)
    finally:
        body.close()
    if len(data) > length:
        raise StorageObjectTooLarge("Storage ignored the requested byte range")
    return data


def download_bytes(key: str, *, max_bytes: int | None = None) -> bytes:
    """Fetch an object into memory with an optional authoritative upper bound.

    Background renderers must pass ``max_bytes``.  A storage object can change
    after a metadata check, so the response length and the actual streamed body
    are both bounded here at the allocation boundary.
    """
    resp = get_s3_client().get_object(Bucket=_storage_options()["bucket_name"], Key=key)
    body = resp["Body"]
    if max_bytes is None:
        return body.read()
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    try:
        content_length = int(resp.get("ContentLength", -1))
    except (TypeError, ValueError):
        content_length = -1
    if content_length > max_bytes:
        body.close()
        raise StorageObjectTooLarge("Storage object exceeds the permitted size")
    data = body.read(max_bytes + 1)
    body.close()
    if len(data) > max_bytes:
        raise StorageObjectTooLarge("Storage object exceeds the permitted size")
    return data


def download_to_path(
    key: str,
    path: str | PathLike[str],
    *,
    max_bytes: int,
    expected_size_bytes: int | None = None,
) -> int:
    """Stream one object to a caller-owned path under authoritative byte bounds."""

    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if expected_size_bytes is not None and (
        isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or not 1 <= expected_size_bytes <= max_bytes
    ):
        raise ValueError("expected_size_bytes must be within max_bytes")

    resp = get_s3_client().get_object(Bucket=_storage_options()["bucket_name"], Key=key)
    body = resp["Body"]
    try:
        try:
            declared_size = int(resp.get("ContentLength", -1))
        except (TypeError, ValueError):
            declared_size = -1
        if declared_size > max_bytes:
            raise StorageObjectTooLarge("Storage object exceeds the permitted size")
        if expected_size_bytes is not None and declared_size not in (-1, expected_size_bytes):
            raise StorageObjectSizeMismatch("Storage object size does not match the record")

        total = 0
        with open(path, "xb") as destination:
            while True:
                remaining = max_bytes - total
                chunk = body.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise OSError("Storage returned a non-byte stream")
                total += len(chunk)
                if total > max_bytes:
                    raise StorageObjectTooLarge("Storage object exceeds the permitted size")
                destination.write(chunk)
        if expected_size_bytes is not None and total != expected_size_bytes:
            raise StorageObjectSizeMismatch("Storage object size does not match the record")
        return total
    finally:
        body.close()


def copy_object(*, src_key: str, dest_key: str) -> str:
    bucket = _storage_options()["bucket_name"]
    get_s3_client().copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": src_key}, Key=dest_key)
    return dest_key


def delete_object(key: str) -> None:
    get_s3_client().delete_object(Bucket=_storage_options()["bucket_name"], Key=key)
