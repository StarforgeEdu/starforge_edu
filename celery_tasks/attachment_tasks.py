"""Bounded lifecycle cleanup for assignment, messaging, and print uploads."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from config.celery import app

logger = logging.getLogger(__name__)

_DOMAINS = {"assignments", "messaging", "printing"}
_BATCH_SIZE = 500


def _active_schemas() -> list[str]:
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    return list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .values_list("schema_name", flat=True)
    )


def _trusted_durable_key(
    domain: str,
    key: object,
    *,
    schema: str,
    grant_id: int | None = None,
) -> bool:
    if domain == "assignments":
        from apps.assignments.storage_keys import (
            parse_final_attachment_key as parse_assignment_final_key,
        )
        from apps.assignments.storage_keys import (
            parse_legacy_attachment_key as parse_assignment_legacy_key,
        )

        return (
            parse_assignment_final_key(key, schema=schema) is not None
            or parse_assignment_legacy_key(key, schema=schema) is not None
        )
    if domain == "messaging":
        from apps.messaging.storage_keys import (
            parse_final_attachment_key as parse_message_final_key,
        )
        from apps.messaging.storage_keys import (
            parse_legacy_attachment_key as parse_message_legacy_key,
        )

        return (
            parse_message_final_key(key, schema=schema) is not None
            or parse_message_legacy_key(key, schema=schema) is not None
        )
    if domain == "printing":
        from apps.printing.storage_keys import parse_final_print_document_key

        parsed = parse_final_print_document_key(key, schema=schema)
        return parsed is not None and grant_id is not None and parsed.grant_id == grant_id
    return False


def _trusted_upload_source(
    domain: str,
    key: object,
    *,
    schema: str,
    owner_id: int | None,
    allow_legacy: bool,
) -> bool:
    """Validate a grant's source namespace, never a durable final namespace."""

    if domain == "assignments":
        from apps.assignments.storage_keys import (
            parse_legacy_attachment_key as parse_assignment_legacy_key,
        )
        from apps.assignments.storage_keys import (
            parse_pending_attachment_key as parse_assignment_pending_key,
        )

        assignment_pending = parse_assignment_pending_key(key, schema=schema)
        if assignment_pending is not None:
            return owner_id is None or assignment_pending.owner_id == owner_id
        # The deployed assignment grammar predates owner ids in object keys;
        # the locked grant row itself remains the ownership boundary.
        return allow_legacy and parse_assignment_legacy_key(key, schema=schema) is not None
    if domain == "messaging":
        from apps.messaging.storage_keys import (
            parse_legacy_attachment_key as parse_message_legacy_key,
        )
        from apps.messaging.storage_keys import (
            parse_pending_attachment_key as parse_message_pending_key,
        )

        message_pending = parse_message_pending_key(key, schema=schema)
        if message_pending is not None:
            return owner_id is None or message_pending.owner_id == owner_id
        legacy = parse_message_legacy_key(key, schema=schema)
        return allow_legacy and legacy is not None and (owner_id is None or legacy.owner_id == owner_id)
    if domain == "printing":
        from apps.printing.storage_keys import parse_pending_print_upload_key

        pending = parse_pending_print_upload_key(key, schema=schema)
        return pending is not None and (owner_id is None or pending.owner_id == owner_id)
    return False


def _legacy_durable_key(domain: str, key: object, *, schema: str) -> bool:
    if domain == "assignments":
        from apps.assignments.storage_keys import (
            parse_legacy_attachment_key as parse_assignment_legacy_key,
        )

        return parse_assignment_legacy_key(key, schema=schema) is not None
    if domain == "messaging":
        from apps.messaging.storage_keys import parse_legacy_attachment_key as parse_message_legacy_key

        return parse_message_legacy_key(key, schema=schema) is not None
    if domain == "printing":
        return False
    return False


def _delete_one(key: str) -> None:
    from infrastructure.storage.s3_client import delete_object

    delete_object(key)


def _grant_model(domain: str):
    if domain == "assignments":
        from apps.assignments.models import AssignmentUploadGrant

        return AssignmentUploadGrant
    if domain == "messaging":
        from apps.messaging.models import MessageAttachmentUploadGrant

        return MessageAttachmentUploadGrant
    if domain == "printing":
        from apps.printing.models import PrintUploadGrant

        return PrintUploadGrant
    raise ValueError("Invalid attachment domain")


def _locked_pending_durable_grant(grant_model, *, grant_id: int):
    return (
        grant_model.objects.select_for_update(skip_locked=True)
        .filter(
            pk=grant_id,
            consumed_at__isnull=False,
            deletion_requested_at__isnull=False,
            durable_deleted_at__isnull=True,
        )
        .first()
    )


@app.task
def delete_attachment_objects(domain: str, grant_ids: list[int]) -> int:
    """Delete transactionally marked objects by opaque grant identifier."""

    from core.utils import current_schema

    if domain not in _DOMAINS or not isinstance(grant_ids, list) or len(grant_ids) > _BATCH_SIZE:
        raise ValueError("Invalid attachment cleanup request")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in grant_ids):
        raise ValueError("Invalid attachment cleanup grant")
    schema = current_schema()
    Grant = _grant_model(domain)
    cleaned = 0
    skipped = 0
    for grant_id in set(grant_ids):
        with transaction.atomic():
            grant = _locked_pending_durable_grant(Grant, grant_id=grant_id)
            if grant is None:
                skipped += 1
                continue
            key = grant.durable_key or grant.key
            if not _trusted_durable_key(domain, key, schema=schema, grant_id=grant.pk):
                logger.warning(
                    "Skipped poisoned durable attachment during cleanup domain=%s schema=%s grant_id=%s",
                    domain,
                    schema,
                    grant.pk,
                )
                grant.durable_deleted_at = timezone.now()
                grant.save(update_fields=["durable_deleted_at"])
                skipped += 1
                continue
            _delete_one(key)
            grant.durable_deleted_at = timezone.now()
            grant.save(update_fields=["durable_deleted_at"])
            cleaned += 1
    if skipped:
        logger.warning(
            "Skipped untracked or untrusted attachment cleanup references domain=%s schema=%s count=%s",
            domain,
            schema,
            skipped,
        )
    return cleaned


@app.task
def cleanup_consumed_upload_sources_for_schema(domain: str, grant_ids: list[int]) -> int:
    """Remove staging objects after their record-bound copy commits."""

    from core.utils import current_schema

    if domain not in _DOMAINS or not isinstance(grant_ids, list) or len(grant_ids) > _BATCH_SIZE:
        raise ValueError("Invalid upload-source cleanup request")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in grant_ids):
        raise ValueError("Invalid upload grant identifier")
    Grant: Any = _grant_model(domain)

    schema = current_schema()
    cleaned = 0
    for grant_id in set(grant_ids):
        # Keep cleanup single-writer and avoid racing the transaction that is
        # consuming this grant into a durable record attachment.
        with transaction.atomic():
            grant = (
                Grant.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=grant_id,
                    consumed_at__isnull=False,
                    source_deleted_at__isnull=True,
                )
                .first()
            )
            if grant is None:
                continue
            if not _trusted_upload_source(
                domain,
                grant.key,
                schema=schema,
                owner_id=grant.requested_by_id,
                allow_legacy=False,
            ):
                logger.warning(
                    "Skipped poisoned upload source during cleanup domain=%s schema=%s grant_id=%s",
                    domain,
                    schema,
                    grant.pk,
                )
                grant.source_deleted_at = timezone.now()
                grant.save(update_fields=["source_deleted_at"])
                continue
            _delete_one(grant.key)
            grant.source_deleted_at = timezone.now()
            grant.save(update_fields=["source_deleted_at"])
            cleaned += 1
    return cleaned


@app.task
def cleanup_expired_attachment_uploads() -> int:
    """Public dispatcher for expired staging grants in active tenant schemas."""

    schemas = _active_schemas()
    for schema in schemas:
        cleanup_expired_attachment_uploads_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def cleanup_expired_attachment_uploads_for_schema() -> int:
    """Bound abandoned sources and retry durable deletion for one tenant."""

    from apps.assignments.models import AssignmentUploadGrant
    from apps.messaging.models import MessageAttachmentUploadGrant
    from apps.printing.models import PrintUploadGrant
    from core.utils import current_schema

    schema = current_schema()
    now = timezone.now()
    cleaned = 0
    for domain, Grant in (
        ("assignments", AssignmentUploadGrant),
        ("messaging", MessageAttachmentUploadGrant),
        ("printing", PrintUploadGrant),
    ):
        grant_ids = list(
            Grant.objects.filter(
                expires_at__lte=now,
                source_deleted_at__isnull=True,
            )
            .order_by("pk")
            .values_list("pk", flat=True)[:_BATCH_SIZE]
        )
        for grant_id in grant_ids:
            with transaction.atomic():
                grant = (
                    Grant.objects.select_for_update(skip_locked=True)
                    .filter(
                        pk=grant_id,
                        expires_at__lte=now,
                        source_deleted_at__isnull=True,
                    )
                    .first()
                )
                if grant is None:
                    continue
                legacy_durable = grant.consumed_at is not None and _legacy_durable_key(
                    domain, grant.key, schema=schema
                )
                if not legacy_durable:
                    if _trusted_upload_source(
                        domain,
                        grant.key,
                        schema=schema,
                        owner_id=grant.requested_by_id,
                        allow_legacy=True,
                    ):
                        _delete_one(grant.key)
                    else:
                        logger.warning(
                            "Skipped poisoned expired upload source domain=%s schema=%s grant_id=%s",
                            domain,
                            schema,
                            grant.pk,
                        )
                if grant.consumed_at is None:
                    grant.delete()
                else:
                    grant.source_deleted_at = now
                    grant.save(update_fields=["source_deleted_at"])
                cleaned += 1
    for domain, Grant in (
        ("assignments", AssignmentUploadGrant),
        ("messaging", MessageAttachmentUploadGrant),
        ("printing", PrintUploadGrant),
    ):
        durable_grant_ids = list(
            Grant.objects.filter(
                deletion_requested_at__isnull=False,
                durable_deleted_at__isnull=True,
            )
            .order_by("pk")
            .values_list("pk", flat=True)[:_BATCH_SIZE]
        )
        for grant_id in durable_grant_ids:
            with transaction.atomic():
                grant = (
                    Grant.objects.select_for_update(skip_locked=True)
                    .filter(
                        pk=grant_id,
                        deletion_requested_at__isnull=False,
                        durable_deleted_at__isnull=True,
                    )
                    .first()
                )
                if grant is None:
                    continue
                key = grant.durable_key or grant.key
                if _trusted_durable_key(domain, key, schema=schema, grant_id=grant.pk):
                    _delete_one(key)
                else:
                    logger.warning(
                        "Skipped poisoned durable attachment during sweep domain=%s schema=%s grant_id=%s",
                        domain,
                        schema,
                        grant.pk,
                    )
                grant.durable_deleted_at = now
                grant.save(update_fields=["durable_deleted_at"])
                cleaned += 1
    return cleaned
