"""Adversarial message attachment storage-boundary regressions."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib import admin
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.messaging import services
from apps.messaging.admin import MessageAdmin, MessageAttachmentUploadGrantAdmin
from apps.messaging.models import Message, MessageAttachmentUploadGrant, Thread
from apps.messaging.storage_keys import final_attachment_key, pending_attachment_key
from apps.users.tests.factories import UserFactory
from core.exceptions import NotFoundException

pytestmark = pytest.mark.django_db


def _grant(*, schema: str, owner, upload_id: str = "b" * 32) -> MessageAttachmentUploadGrant:
    return MessageAttachmentUploadGrant.objects.create(
        key=pending_attachment_key(
            schema=schema,
            owner_id=owner.pk,
            upload_id=upload_id,
            filename="photo.jpg",
        ),
        requested_by=owner,
        content_type="image/jpeg",
        expected_size_bytes=20,
        actual_size_bytes=20,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now(),
    )


def test_download_requires_exact_message_and_grant_binding(tenant_a, monkeypatch):
    signed: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        services,
        "presign_download",
        lambda key, **kwargs: signed.append((key, kwargs)) or "https://storage.invalid/signed",
    )
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        first_thread = Thread.objects.create(created_by=sender)
        other_thread = Thread.objects.create(created_by=sender)
        first = Message.objects.create(thread=first_thread, sender=sender, body="one")
        poisoned = Message.objects.create(thread=other_thread, sender=sender, body="two")
        grant = _grant(schema=tenant_a.schema_name, owner=sender)
        key = final_attachment_key(
            schema=tenant_a.schema_name,
            message_id=first.pk,
            grant_id=grant.pk,
            filename="photo.jpg",
        )
        grant.durable_key = key
        grant.save(update_fields=["durable_key"])
        first.attachments = [key]
        first.save(update_fields=["attachments"])
        poisoned.attachments = [key]
        poisoned.save(update_fields=["attachments"])

        assert services.attachment_download_url(thread=first_thread, key=key).endswith("signed")
        assert signed == [
            (
                key,
                {
                    "expires_in": 300,
                    "download_filename": "photo.jpg",
                    "response_content_type": "image/jpeg",
                },
            )
        ]
        with pytest.raises(NotFoundException):
            services.attachment_download_url(thread=other_thread, key=key)
        assert len(signed) == 1


def test_same_tenant_forged_final_key_is_not_signed(tenant_a, monkeypatch):
    monkeypatch.setattr(
        services,
        "presign_download",
        lambda *_args, **_kwargs: pytest.fail("forged key was signed"),
    )
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        thread = Thread.objects.create(created_by=sender)
        message = Message.objects.create(thread=thread, sender=sender, body="x")
        forged = final_attachment_key(
            schema=tenant_a.schema_name,
            message_id=message.pk,
            grant_id=999_999,
            filename="photo.jpg",
        )
        message.attachments = [forged]
        message.save(update_fields=["attachments"])

        with pytest.raises(NotFoundException):
            services.attachment_download_url(thread=thread, key=forged)


def test_legacy_key_fails_closed_when_copied_between_records(tenant_a, monkeypatch):
    monkeypatch.setattr(services, "presign_download", lambda *_args, **_kwargs: "signed")
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        thread = Thread.objects.create(created_by=sender)
        first = Message.objects.create(thread=thread, sender=sender, body="one")
        legacy = f"{tenant_a.schema_name}/messaging/{sender.pk}/{'c' * 32}/photo.jpg"
        MessageAttachmentUploadGrant.objects.create(
            key=legacy,
            durable_key=legacy,
            requested_by=sender,
            content_type="image/jpeg",
            expected_size_bytes=20,
            actual_size_bytes=20,
            expires_at=timezone.now() - timedelta(days=1),
            consumed_at=timezone.now() - timedelta(days=1),
        )
        first.attachments = [legacy]
        first.save(update_fields=["attachments"])
        assert services.attachment_download_url(thread=thread, key=legacy) == "signed"

        second = Message.objects.create(thread=thread, sender=sender, body="two", attachments=[legacy])
        with pytest.raises(NotFoundException):
            services.attachment_download_url(thread=thread, key=legacy)
        assert services.trusted_message_attachment_keys(second) == ()


def test_durable_message_attachment_survives_sender_hard_delete(tenant_a, monkeypatch):
    monkeypatch.setattr(services, "presign_download", lambda *_args, **_kwargs: "signed")
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        thread = Thread.objects.create(created_by=sender)
        message = Message.objects.create(thread=thread, sender=sender, body="history")
        grant = _grant(schema=tenant_a.schema_name, owner=sender)
        key = final_attachment_key(
            schema=tenant_a.schema_name,
            message_id=message.pk,
            grant_id=grant.pk,
            filename="photo.jpg",
        )
        grant.durable_key = key
        grant.save(update_fields=["durable_key"])
        message.attachments = [key]
        message.save(update_fields=["attachments"])

        sender.delete()
        message.refresh_from_db()
        grant.refresh_from_db()

        assert message.sender_id is None
        assert grant.requested_by_id is None
        assert services.attachment_download_url(thread=thread, key=key) == "signed"


def test_messaging_admin_is_append_only_and_redacts_storage_keys():
    assert "attachments" in MessageAdmin.exclude
    assert "body" in MessageAdmin.exclude
    assert "key" in MessageAttachmentUploadGrantAdmin.exclude
    assert "durable_key" in MessageAttachmentUploadGrantAdmin.exclude
    assert admin.site.is_registered(MessageAttachmentUploadGrant)


def test_expired_sweep_never_deletes_a_consumed_legacy_message_object(tenant_a, monkeypatch):
    from celery_tasks import attachment_tasks

    deleted: list[str] = []
    monkeypatch.setattr(attachment_tasks, "_delete_one", deleted.append)
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        legacy = f"{tenant_a.schema_name}/messaging/{sender.pk}/{'d' * 32}/photo.jpg"
        grant = MessageAttachmentUploadGrant.objects.create(
            key=legacy,
            requested_by=sender,
            content_type="image/jpeg",
            expected_size_bytes=20,
            actual_size_bytes=20,
            expires_at=timezone.now() - timedelta(days=1),
            consumed_at=timezone.now() - timedelta(days=1),
        )

        assert attachment_tasks.cleanup_expired_attachment_uploads_for_schema() == 1
        assert deleted == []
        grant.refresh_from_db()
        assert grant.source_deleted_at is not None


def test_consumed_source_cleanup_never_deletes_a_forged_final_key(tenant_a, monkeypatch):
    from celery_tasks import attachment_tasks

    deleted: list[str] = []
    monkeypatch.setattr(attachment_tasks, "_delete_one", deleted.append)
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        grant = MessageAttachmentUploadGrant.objects.create(
            key=final_attachment_key(
                schema=tenant_a.schema_name,
                message_id=123,
                grant_id=456,
                filename="photo.jpg",
            ),
            requested_by=sender,
            content_type="image/jpeg",
            expected_size_bytes=20,
            actual_size_bytes=20,
            expires_at=timezone.now() - timedelta(days=1),
            consumed_at=timezone.now() - timedelta(days=1),
        )

        assert attachment_tasks.cleanup_consumed_upload_sources_for_schema("messaging", [grant.pk]) == 0
        assert deleted == []
        grant.refresh_from_db()
        assert grant.source_deleted_at is not None


def test_periodic_sweep_retries_a_transactionally_marked_durable_delete(tenant_a, monkeypatch):
    from celery_tasks import attachment_tasks

    deleted: list[str] = []
    monkeypatch.setattr(attachment_tasks, "_delete_one", deleted.append)
    with schema_context(tenant_a.schema_name):
        sender = UserFactory()
        grant = _grant(schema=tenant_a.schema_name, owner=sender)
        key = final_attachment_key(
            schema=tenant_a.schema_name,
            message_id=123,
            grant_id=grant.pk,
            filename="photo.jpg",
        )
        grant.durable_key = key
        grant.source_deleted_at = timezone.now()
        grant.deletion_requested_at = timezone.now()
        grant.save(
            update_fields=[
                "durable_key",
                "source_deleted_at",
                "deletion_requested_at",
            ]
        )

        assert attachment_tasks.cleanup_expired_attachment_uploads_for_schema() == 1
        assert deleted == [key]
        grant.refresh_from_db()
        assert grant.durable_deleted_at is not None
