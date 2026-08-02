"""Adversarial object-key, upload-race, and image-decoding regressions."""

from __future__ import annotations

import io
import struct
import zlib
from typing import Any

import pytest
from django_tenants.utils import schema_context

from apps.content import services
from apps.content.models import FileView, LessonFile
from apps.content.presenters import lesson_file_to_dict
from apps.content.storage_keys import parse_content_key, trusted_file_keys
from apps.content.tests.factories import FolderFactory, LessonFileFactory
from core.exceptions import ConflictException

pytestmark = pytest.mark.django_db


def _png_with_dimensions(width: int, height: int) -> bytes:
    payload = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + payload
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk))
    )


def test_poisoned_pending_key_is_rejected_without_storage_access(tenant_a, monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(services, "head_object", lambda key: calls.append(("head", key)))
    monkeypatch.setattr(services, "delete_object", lambda key: calls.append(("delete", key)))
    monkeypatch.setattr(
        services,
        "copy_object",
        lambda **kwargs: calls.append(("copy", str(kwargs))),
    )

    with schema_context(tenant_a.schema_name):
        file: Any = LessonFileFactory(status=LessonFile.Status.PENDING)
        file.s3_key = f"{tenant_a.schema_name}/content/{file.id + 100}/foreign.pdf"
        file.save(update_fields=["s3_key"])

        assert services.validate_uploaded_file(file.id) == LessonFile.Status.REJECTED
        file.refresh_from_db()
        assert file.reject_reason == "The upload storage reference is invalid."
        assert calls == []


def test_poisoned_clean_key_cannot_be_signed_or_counted(tenant_a, user_in, monkeypatch):
    signed: list[str] = []
    monkeypatch.setattr(
        services,
        "presign_download",
        lambda key, **_kwargs: signed.append(key) or "https://should-not-be-issued.invalid",
    )
    user = user_in(tenant_a, roles=["director"])

    with schema_context(tenant_a.schema_name):
        file: Any = LessonFileFactory(status=LessonFile.Status.CLEAN)
        file.s3_key = f"{tenant_a.schema_name}/content/{file.id + 1}/other.pdf"
        file.save(update_fields=["s3_key"])

        with pytest.raises(ConflictException) as exc:
            services.download_url(file=file, user=user)

        assert exc.value.code == "file_unavailable"
        file.refresh_from_db()
        assert file.download_count == 0
        assert FileView.objects.filter(file=file).count() == 0
        assert signed == []


def test_poisoned_thumbnail_is_not_presigned(tenant_a, monkeypatch):
    signed: list[str] = []
    import infrastructure.storage.s3_client as s3_client

    monkeypatch.setattr(
        s3_client,
        "presign_download",
        lambda key, **_kwargs: signed.append(key) or "https://should-not-be-issued.invalid",
    )
    with schema_context(tenant_a.schema_name):
        file: Any = LessonFileFactory(content_type="image/png")
        file.thumbnail_key = f"{tenant_a.schema_name}/content/{file.id + 1}/thumb.jpg"
        file.save(update_fields=["thumbnail_key"])

        assert lesson_file_to_dict(file)["thumbnail_url"] is None
        assert signed == []


def test_uploaded_thumb_filename_cannot_collide_with_derived_thumbnail(
    tenant_a,
    s3_stub,
    monkeypatch,
):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, format="JPEG")
    image_bytes = buffer.getvalue()
    monkeypatch.setattr(services, "_sniff_mime", lambda _buffer: "image/jpeg")

    with schema_context(tenant_a.schema_name):
        result = services.request_upload(
            filename="thumb.jpg",
            content_type="image/jpeg",
            size_bytes=len(image_bytes),
            folder=FolderFactory(),
        )
        file = result["file"]
        s3_stub.put(file.s3_key, image_bytes)

        assert services.validate_uploaded_file(file.id) == LessonFile.Status.CLEAN
        file.refresh_from_db()
        primary = file.s3_key
        assert primary == f"{tenant_a.schema_name}/content/{file.id}/thumb.jpg"

        derived = services.generate_thumbnail(file.id)
        assert derived == f"{tenant_a.schema_name}/content/{file.id}/_derived/thumbnail.jpg"
        assert derived != primary
        assert s3_stub.objects[primary] == image_bytes


def test_decompression_bomb_copy_is_quarantined_and_removed(tenant_a, monkeypatch):
    bomb = _png_with_dimensions(100_000, 100_000)
    copied: list[str] = []
    deleted: list[str] = []
    monkeypatch.setattr(services, "presign_upload", lambda key, **_kwargs: f"https://put/{key}")

    with schema_context(tenant_a.schema_name):
        file = services.request_upload(
            filename="large.png",
            content_type="image/png",
            size_bytes=len(bomb),
            folder=FolderFactory(),
        )["file"]
        source_key = file.s3_key
        final_key = f"{tenant_a.schema_name}/content/{file.id}/large.png"
        objects = {source_key: bomb}

        monkeypatch.setattr(services, "head_object", lambda key: {"ContentLength": len(objects[key])})
        monkeypatch.setattr(services, "get_object_range", lambda key, **_kwargs: objects[key][:8192])
        monkeypatch.setattr(services, "_sniff_mime", lambda _buffer: "image/png")
        monkeypatch.setattr(services, "download_bytes", lambda key, **_kwargs: objects[key])

        def copy(*, src_key, dest_key):
            objects[dest_key] = objects[src_key]
            copied.append(dest_key)

        def delete(key):
            objects.pop(key, None)
            deleted.append(key)

        monkeypatch.setattr(services, "copy_object", copy)
        monkeypatch.setattr(services, "delete_object", delete)

        assert services.validate_uploaded_file(file.id) == LessonFile.Status.REJECTED
        file.refresh_from_db()
        assert "dimension limit" in file.reject_reason
        assert copied == [final_key]
        assert set(deleted) == {source_key, final_key}
        assert objects == {}


def test_cleanup_grammar_rejects_other_tenant_and_unbound_paths(tenant_a):
    schema = tenant_a.schema_name
    with schema_context(schema):
        file: Any = LessonFileFactory()
        file.thumbnail_key = f"{schema}/content/{file.id}/_derived/thumbnail.jpg"
        file.save(update_fields=["thumbnail_key"])

        assert trusted_file_keys(file, schema=schema) == (file.s3_key, file.thumbnail_key)
        assert parse_content_key(file.s3_key, schema=schema) is not None
        assert parse_content_key(file.thumbnail_key, schema=schema) is not None
        assert parse_content_key(f"other/content/{file.id}/file.pdf", schema=schema) is None
        assert parse_content_key(f"{schema}/reports/{file.id}.pdf", schema=schema) is None
        assert parse_content_key(f"{schema}/content/../../secret", schema=schema) is None
