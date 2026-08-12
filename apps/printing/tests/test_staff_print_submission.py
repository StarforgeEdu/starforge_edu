"""Production contract tests for staff-selected library and phone-file printing."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.printing import services
from apps.printing.models import PrintJob, PrintUploadGrant
from apps.printing.source_resolver import is_print_job_source_valid
from apps.printing.storage_keys import (
    final_print_document_key,
    parse_final_print_document_key,
    parse_pending_print_upload_key,
)
from core.permissions import Role

pytestmark = pytest.mark.django_db

JOBS_URL = "/api/v1/printing/jobs/"
UPLOAD_URL = "/api/v1/printing/upload-url/"


def test_library_file_uses_selected_printer_and_waits_until_schedule(as_role, tenant_a, monkeypatch):
    from apps.content.tests.factories import LessonFileFactory
    from apps.org.tests.factories import BranchFactory
    from apps.printing.tests.factories import BranchAgentFactory, PrinterFactory

    client, _user = as_role(Role.DIRECTOR)
    scheduled = timezone.now() + timedelta(hours=2)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        printer = PrinterFactory(
            branch=branch,
            capabilities={"color": True, "duplex": True, "paper": ["A4"]},
        )
        file = LessonFileFactory(content_type="application/pdf", is_downloadable=True)
        agent = BranchAgentFactory(branch=branch)

    monkeypatch.setattr(
        "apps.printing.document_inspection.authoritative_page_count",
        lambda **_kwargs: 8,
    )
    response = client.post(
        JOBS_URL,
        {
            "source": "content",
            "source_id": file.pk,
            "printer": printer.pk,
            "copies": 20,
            "color": True,
            "duplex": True,
            "scheduled_for": scheduled.isoformat(),
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    data = response.json()["data"]
    assert data["printer"] == printer.pk
    assert data["preferred_printer"] == printer.pk
    assert data["scheduled_for"] == scheduled.isoformat()
    assert data["pages"] == 8

    with schema_context(tenant_a.schema_name):
        job = PrintJob.objects.get(pk=data["id"])
        assert services.claim_job(agent=agent) is None
        job.next_attempt_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["next_attempt_at"])
        claimed = services.claim_job(agent=agent)
        assert claimed is not None
        assert claimed.pk == job.pk
        assert claimed.printer_id == printer.pk


def test_library_file_must_be_downloadable_printable_and_printer_compatible(as_role, tenant_a, monkeypatch):
    from apps.content.tests.factories import LessonFileFactory
    from apps.org.tests.factories import BranchFactory
    from apps.printing.tests.factories import PrinterFactory

    client, _user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        mono = PrinterFactory(branch=branch, capabilities={"color": False, "duplex": False})
        protected = LessonFileFactory(is_downloadable=False)
        printable = LessonFileFactory(is_downloadable=True)

    protected_response = client.post(
        JOBS_URL,
        {"source": "content", "source_id": protected.pk, "printer": mono.pk, "pages": 1},
        format="json",
    )
    assert protected_response.status_code == 422
    assert protected_response.json()["code"] == "print_source_not_ready"

    monkeypatch.setattr(
        "apps.printing.document_inspection.authoritative_page_count",
        lambda **_kwargs: 1,
    )
    incompatible = client.post(
        JOBS_URL,
        {
            "source": "content",
            "source_id": printable.pk,
            "printer": mono.pk,
            "pages": 1,
            "color": True,
        },
        format="json",
    )
    assert incompatible.status_code == 400
    assert incompatible.json()["code"] == "printer_incompatible"


def test_upload_grant_is_closed_owner_bound_exact_size_policy(
    as_role,
    tenant_a,
    monkeypatch,
):
    from apps.org.tests.factories import BranchFactory
    from infrastructure.storage import s3_client

    client, user = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()

    monkeypatch.setattr(
        s3_client,
        "presign_post_upload",
        lambda key, **_kwargs: {
            "url": "https://uploads.example.test/",
            "fields": {"key": key, "policy": "opaque"},
        },
    )
    response = client.post(
        UPLOAD_URL,
        {
            "branch": branch.pk,
            "filename": "lesson.pdf",
            "content_type": "application/pdf",
            "size_bytes": 4096,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    data = response.json()["data"]
    assert data["method"] == "POST"
    assert data["url"] == "https://uploads.example.test/"
    assert {"key", "policy"} <= set(data["fields"])
    assert "key" not in data

    with schema_context(tenant_a.schema_name):
        grant = PrintUploadGrant.objects.get(pk=data["grant_id"])
        parsed = parse_pending_print_upload_key(grant.key, schema=tenant_a.schema_name)
        assert parsed is not None
        assert parsed.owner_id == user.pk
        assert grant.branch_id == branch.pk
        assert grant.expected_size_bytes == 4096

    unsupported = client.post(
        UPLOAD_URL,
        {
            "branch": branch.pk,
            "filename": "clip.mp4",
            "content_type": "video/mp4",
            "size_bytes": 4096,
        },
        format="json",
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["code"] == "file_type_not_printable"

    unknown = client.post(
        UPLOAD_URL,
        {
            "branch": branch.pk,
            "filename": "lesson.pdf",
            "content_type": "application/pdf",
            "size_bytes": 4096,
            "payload_s3_key": "another-tenant/private.pdf",
        },
        format="json",
    )
    assert unknown.status_code == 400
    assert "payload_s3_key" in unknown.json()["errors"]


def test_owned_upload_is_verified_promoted_and_idempotently_enqueued(
    as_role,
    tenant_a,
    monkeypatch,
):
    from apps.org.tests.factories import BranchFactory
    from apps.printing.tests.factories import PrinterFactory
    from core.attachment_storage import VerifiedAttachment
    from infrastructure.storage import s3_client

    client, user = as_role(Role.DIRECTOR)
    scheduled = timezone.now() + timedelta(minutes=30)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        printer = PrinterFactory(branch=branch, capabilities={"color": True, "duplex": True})
        grant = PrintUploadGrant.objects.create(
            branch=branch,
            requested_by=user,
            key=(f"{tenant_a.schema_name}/printing/uploads/{user.pk}/{'a' * 32}/worksheet.pdf"),
            filename="worksheet.pdf",
            content_type="application/pdf",
            expected_size_bytes=8192,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    promoted: list[tuple[str, str]] = []

    def promote(*, source_key, destination_key, **_kwargs):
        promoted.append((source_key, destination_key))
        return VerifiedAttachment(
            size_bytes=8192,
            content_type="application/pdf",
            sniffed_type="application/pdf",
        )

    monkeypatch.setattr("core.attachment_storage.promote_attachment_object", promote)
    monkeypatch.setattr(
        "apps.printing.document_inspection.authoritative_page_count",
        lambda **_kwargs: 10,
    )
    monkeypatch.setattr(s3_client, "delete_object", lambda _key: None)
    payload = {
        "source": "upload",
        "source_id": grant.pk,
        "printer": printer.pk,
        "copies": 20,
        "duplex": True,
        "scheduled_for": scheduled.isoformat(),
    }
    response = client.post(JOBS_URL, payload, format="json")
    assert response.status_code == 201, response.content
    retry = client.post(JOBS_URL, payload, format="json")
    assert retry.status_code == 201, retry.content
    assert retry.json()["data"]["id"] == response.json()["data"]["id"]
    assert response.json()["data"]["pages"] == 10
    assert len(promoted) == 1

    with schema_context(tenant_a.schema_name):
        grant.refresh_from_db()
        job = PrintJob.objects.get(pk=response.json()["data"]["id"])
        expected = final_print_document_key(
            schema=tenant_a.schema_name,
            grant_id=grant.pk,
            filename=grant.filename,
        )
        assert grant.consumed_at is not None
        assert grant.actual_size_bytes == 8192
        assert grant.durable_key == expected == job.payload_s3_key
        assert job.source == PrintJob.Source.UPLOAD
        assert job.preferred_printer_id == printer.pk
        assert is_print_job_source_valid(job) is True
        parsed = parse_final_print_document_key(expected, schema=tenant_a.schema_name)
        assert parsed is not None
        assert parsed.grant_id == grant.pk

    conflict = client.post(JOBS_URL, {**payload, "copies": 19}, format="json")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "print_idempotency_conflict"

    mismatched_pages = client.post(JOBS_URL, {**payload, "pages": 1}, format="json")
    assert mismatched_pages.status_code == 400
    assert mismatched_pages.json()["code"] == "page_count_mismatch"

    with schema_context(tenant_a.schema_name):
        job.status = PrintJob.Status.DONE
        job.next_attempt_at = None
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "next_attempt_at", "finished_at"])
    reused = client.post(JOBS_URL, payload, format="json")
    assert reused.status_code == 409
    assert reused.json()["code"] == "print_upload_already_used"


def test_upload_grant_cannot_be_used_by_another_account(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.printing.tests.factories import PrinterFactory

    owner = user_in(tenant_a, roles=[Role.DIRECTOR])
    attacker = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        printer = PrinterFactory(branch=branch)
        grant = PrintUploadGrant.objects.create(
            branch=branch,
            requested_by=owner,
            key=f"{tenant_a.schema_name}/printing/uploads/{owner.pk}/{'b' * 32}/private.pdf",
            filename="private.pdf",
            content_type="application/pdf",
            expected_size_bytes=100,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    response = as_user(tenant_a, attacker).post(
        JOBS_URL,
        {
            "source": "upload",
            "source_id": grant.pk,
            "printer": printer.pk,
            "pages": 1,
        },
        format="json",
    )
    assert response.status_code == 404
    with schema_context(tenant_a.schema_name):
        grant.refresh_from_db()
        assert grant.consumed_at is None
        assert not PrintJob.objects.filter(source=PrintJob.Source.UPLOAD, source_id=grant.pk).exists()


def test_scoped_staff_cannot_issue_upload_grant_for_another_branch(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        own_branch = BranchFactory()
        foreign_branch = BranchFactory()
    teacher = user_in(tenant_a, roles=[Role.TEACHER], branch=own_branch)

    response = as_user(tenant_a, teacher).post(
        UPLOAD_URL,
        {
            "branch": foreign_branch.pk,
            "filename": "private.pdf",
            "content_type": "application/pdf",
            "size_bytes": 100,
        },
        format="json",
    )
    assert response.status_code == 403
    with schema_context(tenant_a.schema_name):
        assert not PrintUploadGrant.objects.filter(requested_by=teacher).exists()


def test_terminal_uploaded_job_marks_durable_object_for_bounded_cleanup(
    tenant_a,
    user_in,
    monkeypatch,
):
    from apps.org.tests.factories import BranchFactory
    from apps.printing.tests.factories import BranchAgentFactory, PrinterFactory
    from celery_tasks import attachment_tasks

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    deleted: list[str] = []
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        printer = PrinterFactory(branch=branch)
        agent = BranchAgentFactory(branch=branch)
        grant = PrintUploadGrant.objects.create(
            branch=branch,
            requested_by=user,
            key=f"{tenant_a.schema_name}/printing/uploads/{user.pk}/{'c' * 32}/notes.pdf",
            filename="notes.pdf",
            content_type="application/pdf",
            expected_size_bytes=100,
            actual_size_bytes=100,
            expires_at=timezone.now() + timedelta(minutes=5),
            consumed_at=timezone.now(),
        )
        grant.durable_key = final_print_document_key(
            schema=tenant_a.schema_name,
            grant_id=grant.pk,
            filename=grant.filename,
        )
        grant.save(update_fields=["durable_key"])
        job = PrintJob.objects.create(
            branch=branch,
            printer=printer,
            preferred_printer=printer,
            source=PrintJob.Source.UPLOAD,
            source_id=grant.pk,
            payload_s3_key=grant.durable_key,
            pages=2,
            copies=1,
            requested_by=user,
            next_attempt_at=timezone.now(),
        )
        claimed = services.claim_job(agent=agent)
        assert claimed is not None
        services.update_job_status(
            agent=agent,
            job_id=job.pk,
            lease_id=claimed.lease_id,
            status=PrintJob.Status.PRINTING,
        )
        completed = services.update_job_status(
            agent=agent,
            job_id=job.pk,
            lease_id=claimed.lease_id,
            status=PrintJob.Status.DONE,
            pages_printed=2,
        )
        assert completed.status == PrintJob.Status.DONE
        grant.refresh_from_db()
        assert grant.deletion_requested_at is not None

        monkeypatch.setattr(attachment_tasks, "_delete_one", deleted.append)
        assert attachment_tasks.delete_attachment_objects("printing", [grant.pk]) == 1
        grant.refresh_from_db()
        assert grant.durable_deleted_at is not None
        assert deleted == [grant.durable_key]


def test_invalid_upload_job_cannot_schedule_another_grants_object_for_deletion(
    tenant_a,
    user_in,
):
    from apps.org.tests.factories import BranchFactory
    from apps.printing.tests.factories import BranchAgentFactory

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        grant_branch = BranchFactory()
        poisoned_branch = BranchFactory()
        agent = BranchAgentFactory(branch=poisoned_branch)
        grant = PrintUploadGrant.objects.create(
            branch=grant_branch,
            requested_by=user,
            key=f"{tenant_a.schema_name}/printing/uploads/{user.pk}/{'d' * 32}/private.pdf",
            filename="private.pdf",
            content_type="application/pdf",
            expected_size_bytes=100,
            actual_size_bytes=100,
            expires_at=timezone.now() + timedelta(minutes=5),
            consumed_at=timezone.now(),
        )
        grant.durable_key = final_print_document_key(
            schema=tenant_a.schema_name,
            grant_id=grant.pk,
            filename=grant.filename,
        )
        grant.save(update_fields=["durable_key"])
        poisoned = PrintJob.objects.create(
            branch=poisoned_branch,
            source=PrintJob.Source.UPLOAD,
            source_id=grant.pk,
            payload_s3_key=grant.durable_key,
            pages=1,
            requested_by=user,
            next_attempt_at=timezone.now(),
        )

        claimed = services.claim_job(agent=agent)
        assert claimed is not None
        assert is_print_job_source_valid(claimed) is False
        services.reject_invalid_claim(agent=agent, job_id=poisoned.pk)

        grant.refresh_from_db()
        assert grant.deletion_requested_at is None


def test_cleanup_rejects_durable_key_bound_to_a_different_grant(
    tenant_a,
    user_in,
    monkeypatch,
):
    from apps.org.tests.factories import BranchFactory
    from celery_tasks import attachment_tasks

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    deleted: list[str] = []
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        grant = PrintUploadGrant.objects.create(
            branch=branch,
            requested_by=user,
            key=f"{tenant_a.schema_name}/printing/uploads/{user.pk}/{'e' * 32}/private.pdf",
            filename="private.pdf",
            content_type="application/pdf",
            expected_size_bytes=100,
            actual_size_bytes=100,
            expires_at=timezone.now() + timedelta(minutes=5),
            consumed_at=timezone.now(),
            deletion_requested_at=timezone.now(),
        )
        grant.durable_key = final_print_document_key(
            schema=tenant_a.schema_name,
            grant_id=grant.pk + 1000,
            filename=grant.filename,
        )
        grant.save(update_fields=["durable_key"])

        monkeypatch.setattr(attachment_tasks, "_delete_one", deleted.append)
        assert attachment_tasks.delete_attachment_objects("printing", [grant.pk]) == 0

        grant.refresh_from_db()
        assert grant.durable_deleted_at is not None
        assert deleted == []
