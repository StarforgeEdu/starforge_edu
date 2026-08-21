"""Adversarial assignment attachment storage-boundary regressions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.contrib import admin
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.assignments import services
from apps.assignments.admin import AssignmentAdmin, AssignmentUploadGrantAdmin, SubmissionAdmin
from apps.assignments.models import AssignmentUploadGrant, Submission
from apps.assignments.storage_keys import final_attachment_key, pending_attachment_key
from apps.assignments.tests.factories import AssignmentFactory
from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.users.tests.factories import UserFactory
from core.attachment_storage import AttachmentObjectError, VerifiedAttachment
from core.exceptions import UnprocessableEntity

pytestmark = pytest.mark.django_db


def _submission_target():
    branch = BranchFactory()
    cohort = CohortFactory(branch=branch)
    student = StudentProfileFactory(branch=branch)
    CohortMembershipFactory(cohort=cohort, student=student)
    assignment: Any = AssignmentFactory(cohort=cohort)
    return assignment, student


def _grant(*, schema: str, owner, consumed: bool = False) -> AssignmentUploadGrant:
    key = pending_attachment_key(
        schema=schema,
        owner_id=owner.pk,
        upload_id="a" * 32,
        filename="evidence.pdf",
    )
    return AssignmentUploadGrant.objects.create(
        key=key,
        requested_by=owner,
        content_type="application/pdf",
        expected_size_bytes=12,
        expires_at=timezone.now() + timedelta(minutes=5),
        consumed_at=timezone.now() if consumed else None,
        actual_size_bytes=12 if consumed else None,
    )


def test_submission_promotion_uses_a_non_uploadable_record_bound_destination(
    tenant_a,
    monkeypatch,
):
    copied: list[dict] = []

    def promote(**kwargs):
        copied.append(kwargs)
        return VerifiedAttachment(12, "application/pdf", "application/pdf")

    monkeypatch.setattr(services, "promote_attachment_object", promote)
    with schema_context(tenant_a.schema_name):
        assignment, student = _submission_target()
        grant = _grant(schema=tenant_a.schema_name, owner=student.user)

        submission = services.submit(
            assignment=assignment,
            student=student,
            attachment_keys=[grant.key],
            actor=student.user,
        )

        expected = final_attachment_key(
            schema=tenant_a.schema_name,
            target_kind="submissions",
            target_id=submission.pk,
            grant_id=grant.pk,
            filename="evidence.pdf",
        )
        assert submission.attachments == [expected]
        assert copied == [
            {
                "source_key": grant.key,
                "destination_key": expected,
                "filename": "evidence.pdf",
                "expected_size_bytes": 12,
                "expected_content_type": "application/pdf",
            }
        ]
        grant.refresh_from_db()
        assert grant.consumed_at is not None


def test_cross_record_and_forged_final_keys_are_never_trusted(tenant_a):
    with schema_context(tenant_a.schema_name):
        assignment, student = _submission_target()
        first = Submission.objects.create(assignment=assignment, student=student, attachments=[])
        second = Submission.objects.create(
            assignment=assignment,
            student=student,
            attempt_number=2,
            attachments=[],
        )
        grant = _grant(schema=tenant_a.schema_name, owner=student.user, consumed=True)
        owned = final_attachment_key(
            schema=tenant_a.schema_name,
            target_kind="submissions",
            target_id=first.pk,
            grant_id=grant.pk,
            filename="evidence.pdf",
        )
        forged = final_attachment_key(
            schema=tenant_a.schema_name,
            target_kind="submissions",
            target_id=second.pk,
            grant_id=grant.pk + 10_000,
            filename="evidence.pdf",
        )
        first.attachments = [owned]
        first.save(update_fields=["attachments"])
        second.attachments = [owned, forged]
        second.save(update_fields=["attachments"])

        assert services.trusted_attachment_keys(first) == (owned,)
        assert services.trusted_attachment_keys(second) == ()


def test_failed_content_verification_does_not_consume_or_persist_a_key(tenant_a, monkeypatch):
    monkeypatch.setattr(
        services,
        "promote_attachment_object",
        lambda **_kwargs: (_ for _ in ()).throw(AttachmentObjectError("content")),
    )
    with schema_context(tenant_a.schema_name):
        assignment, student = _submission_target()
        grant = _grant(schema=tenant_a.schema_name, owner=student.user)

        with pytest.raises(UnprocessableEntity) as exc:
            services.submit(
                assignment=assignment,
                student=student,
                attachment_keys=[grant.key],
                actor=student.user,
            )

        assert exc.value.code == "attachment_content_mismatch"
        grant.refresh_from_db()
        assert grant.consumed_at is None
        assert not Submission.objects.filter(assignment=assignment, student=student).exists()


def test_durable_assignment_attachment_survives_uploader_hard_delete(tenant_a):
    with schema_context(tenant_a.schema_name):
        assignment, _student = _submission_target()
        # Use a deliberately profile-less legacy uploader. Student identities are
        # immutable history and correctly protect their compatibility User from
        # hard deletion; the SET_NULL durability contract is about deletable User
        # rows, not bypassing that identity safeguard.
        uploader = UserFactory()
        grant = _grant(schema=tenant_a.schema_name, owner=uploader, consumed=True)
        key = final_attachment_key(
            schema=tenant_a.schema_name,
            target_kind="assignments",
            target_id=assignment.pk,
            grant_id=grant.pk,
            filename="evidence.pdf",
        )
        grant.durable_key = key
        grant.save(update_fields=["durable_key"])
        assignment.attachments = [key]
        assignment.save(update_fields=["attachments"])

        uploader.delete()
        grant.refresh_from_db()

        assert grant.requested_by_id is None
        assert services.trusted_attachment_keys(assignment) == (key,)


def test_assignment_admin_cannot_edit_or_reveal_storage_keys():
    assert "attachments" in AssignmentAdmin.exclude
    assert "attachments" in SubmissionAdmin.exclude
    assert "key" in AssignmentUploadGrantAdmin.exclude
    assert "durable_key" in AssignmentUploadGrantAdmin.exclude
    assert admin.site.is_registered(AssignmentUploadGrant)


def test_expired_cleanup_never_deletes_a_poisoned_cross_tenant_key(tenant_a, monkeypatch):
    from celery_tasks import attachment_tasks

    deleted: list[str] = []
    monkeypatch.setattr(attachment_tasks, "_delete_one", deleted.append)
    with schema_context(tenant_a.schema_name):
        owner = StudentProfileFactory().user
        poisoned = AssignmentUploadGrant.objects.create(
            key="another_tenant/assignments/uploads/1/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/secret.pdf",
            requested_by=owner,
            content_type="application/pdf",
            expected_size_bytes=10,
            expires_at=timezone.now() - timedelta(hours=1),
        )

        assert attachment_tasks.cleanup_expired_attachment_uploads_for_schema() == 1
        assert deleted == []
        assert not AssignmentUploadGrant.objects.filter(pk=poisoned.pk).exists()
