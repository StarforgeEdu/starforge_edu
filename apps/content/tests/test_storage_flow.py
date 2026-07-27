"""D2-F-4: full signed-URL flow against the shared in-memory S3 stub
(pending → clean and pending → rejected), plus a live-MinIO round trip that
auto-skips when unreachable."""

from __future__ import annotations

import os
from typing import Any

import pytest
from django_tenants.utils import schema_context

from apps.content import services
from apps.content.models import LessonFile
from apps.content.tests.factories import FolderFactory

pytestmark = pytest.mark.django_db


def _upload(folder, *, content_type="application/pdf", filename="doc.pdf"):
    return services.request_upload(
        filename=filename, content_type=content_type, size_bytes=2048, folder=folder
    )["file"]


def test_full_flow_pending_to_clean(tenant_a, s3_stub, monkeypatch):
    monkeypatch.setattr(services, "_sniff_mime", lambda buf: "application/pdf")
    with schema_context(tenant_a.schema_name):
        folder: Any = FolderFactory()
        file = _upload(folder)
        # Browser PUTs the bytes to the presigned tmp key.
        s3_stub.put(file.s3_key, b"%PDF-1.4 fake")
        tmp_key = file.s3_key

        services.confirm_upload(file=file)  # 202 enqueue (no S3 here)
        services.validate_uploaded_file(file.id)

        file.refresh_from_db()
        assert file.status == LessonFile.Status.CLEAN
        assert file.s3_key == f"{tenant_a.schema_name}/content/{file.id}/doc.pdf"
        assert (tmp_key, file.s3_key) in s3_stub.copies
        assert tmp_key in s3_stub.deletes
        assert file.s3_key in s3_stub.objects


def test_full_flow_pending_to_rejected(tenant_a, s3_stub, monkeypatch):
    monkeypatch.setattr(services, "_sniff_mime", lambda buf: "image/png")  # mislabeled
    with schema_context(tenant_a.schema_name):
        folder: Any = FolderFactory()
        file = _upload(folder)  # declared application/pdf
        s3_stub.put(file.s3_key, b"\x89PNG fake")
        tmp_key = file.s3_key

        services.validate_uploaded_file(file.id)

        file.refresh_from_db()
        assert file.status == LessonFile.Status.REJECTED
        assert file.s3_key == tmp_key  # not moved
        assert s3_stub.copies == []


@pytest.mark.minio
def test_minio_live_round_trip():
    """Live MinIO round trip. Set STARFORGE_RUN_MINIO=1 with `docker compose up
    minio` to run; auto-skips otherwise (and in CI without the service)."""
    if not os.environ.get("STARFORGE_RUN_MINIO"):
        pytest.skip("MinIO live test disabled (set STARFORGE_RUN_MINIO=1 with compose up)")
    boto3 = pytest.importorskip("boto3")
    from botocore.client import Config

    endpoint = os.environ.get("MINIO_URL", "http://localhost:9000")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("MINIO_ROOT_USER", "minioadmin"),
        aws_secret_access_key=os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"),
        region_name="us-east-1",
        config=Config(connect_timeout=2, retries={"max_attempts": 0}),
    )
    try:
        client.list_buckets()
    except Exception as exc:  # connection refused / DNS — skip cleanly
        pytest.skip(f"MinIO unreachable at {endpoint}: {exc}")

    bucket, key = "starforge-test", "tmp/roundtrip.txt"
    existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)
    client.put_object(Bucket=bucket, Key=key, Body=b"hello")
    assert client.get_object(Bucket=bucket, Key=key)["Body"].read() == b"hello"
    client.delete_object(Bucket=bucket, Key=key)
