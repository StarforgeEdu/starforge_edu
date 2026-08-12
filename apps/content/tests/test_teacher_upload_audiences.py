"""Exact-principal contracts for teacher library upload audiences."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.content.models import ContentLibrary, LessonFile
from apps.content.tests.factories import ContentLibraryFactory, FolderFactory
from core.permissions import Role
from tests.role_principal_helpers import ensure_role_principal, exact_session_client

pytestmark = pytest.mark.django_db

UPLOAD = "/api/v1/content/upload-url/"
FILES = "/api/v1/content/files/"


def _stub_upload(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.content.services.presign_upload",
        lambda key, **_kwargs: f"https://storage.invalid/{key}",
    )


def _exact_actor(*, tenant, user_in, client_for, role: str, branch=None):
    user = user_in(tenant, roles=[role], branch=branch)
    with schema_context(tenant.schema_name):
        membership_branch = user.role_memberships.get().branch
        profile = ensure_role_principal(user, roles=[role], branch=membership_branch)
    return exact_session_client(client_for, tenant, user), user, profile


def _upload_body(*, folder_id: int, audience: str, downloadable: bool = True) -> dict[str, Any]:
    return {
        "filename": "lesson-notes.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1200,
        "title": "Lesson notes",
        "folder": folder_id,
        "audience": audience,
        "is_downloadable": downloadable,
    }


def test_own_students_requires_a_cohort_the_exact_teacher_teaches(
    tenant_a,
    user_in,
    client_for,
    monkeypatch,
):
    from apps.cohorts.tests.factories import CohortFactory

    _stub_upload(monkeypatch)
    client, _user, teacher = _exact_actor(
        tenant=tenant_a,
        user_in=user_in,
        client_for=client_for,
        role=Role.TEACHER,
    )
    with schema_context(tenant_a.schema_name):
        own_cohort = CohortFactory(branch=teacher.branch, primary_teacher=teacher)
        other_cohort = CohortFactory(branch=teacher.branch)
        own_folder = FolderFactory(
            library=ContentLibraryFactory(
                visibility=ContentLibrary.Visibility.COHORT,
                cohort=own_cohort,
            )
        )
        other_folder = FolderFactory(
            library=ContentLibraryFactory(
                visibility=ContentLibrary.Visibility.COHORT,
                cohort=other_cohort,
            )
        )

    accepted = client.post(
        UPLOAD,
        _upload_body(folder_id=own_folder.pk, audience="own_students", downloadable=False),
        format="json",
    )
    denied = client.post(
        UPLOAD,
        _upload_body(folder_id=other_folder.pk, audience="own_students"),
        format="json",
    )

    assert accepted.status_code == 200, accepted.content
    assert denied.status_code == 400
    with schema_context(tenant_a.schema_name):
        created = LessonFile.objects.get(pk=accepted.json()["data"]["file_id"])
        assert created.folder_id == own_folder.pk
        assert created.submitted_by_teacher_id == teacher.pk
        assert created.submission_audience == LessonFile.SubmissionAudience.OWN_STUDENTS
        assert created.is_downloadable is False
        assert created.is_approved_teacher is False
        assert created.is_approved_manager is False


def test_global_teacher_draft_is_owner_scoped_until_distinct_publication(
    tenant_a,
    user_in,
    client_for,
    monkeypatch,
):
    _stub_upload(monkeypatch)
    owner, owner_user, _teacher = _exact_actor(
        tenant=tenant_a,
        user_in=user_in,
        client_for=client_for,
        role=Role.TEACHER,
    )
    outsider, _outsider_user, _ = _exact_actor(
        tenant=tenant_a,
        user_in=user_in,
        client_for=client_for,
        role=Role.TEACHER,
    )
    publisher, _publisher_user, _ = _exact_actor(
        tenant=tenant_a,
        user_in=user_in,
        client_for=client_for,
        role=Role.DIRECTOR,
    )
    learner, _learner_user, _ = _exact_actor(
        tenant=tenant_a,
        user_in=user_in,
        client_for=client_for,
        role=Role.STUDENT,
    )
    with schema_context(tenant_a.schema_name):
        folder = FolderFactory(library=ContentLibraryFactory(visibility=ContentLibrary.Visibility.TENANT))

    response = owner.post(
        UPLOAD,
        _upload_body(folder_id=folder.pk, audience="global", downloadable=False),
        format="json",
    )
    assert response.status_code == 200, response.content
    file_id = response.json()["data"]["file_id"]

    # The global target does not make an unreviewed draft global. Only its exact
    # uploader and organization-wide publisher can reach the workflow row.
    assert [row["id"] for row in owner.get(FILES).json()["data"]] == [file_id]
    assert all(row["id"] != file_id for row in outsider.get(FILES).json()["data"])
    assert all(row["id"] != file_id for row in learner.get(FILES).json()["data"])
    assert outsider.post(f"{FILES}{file_id}/confirm/", {}, format="json").status_code == 404
    assert owner.post(f"{FILES}{file_id}/confirm/", {}, format="json").status_code == 202

    # Simulate the async object-signature validation completing successfully,
    # then exercise both existing maker-checker legs.
    with schema_context(tenant_a.schema_name):
        LessonFile.objects.filter(pk=file_id).update(status=LessonFile.Status.CLEAN)
    first = owner.post(f"{FILES}{file_id}/approve-teacher/", {}, format="json")
    assert first.status_code == 200, first.content
    assert outsider.post(f"{FILES}{file_id}/approve-teacher/", {}, format="json").status_code == 404
    second = publisher.post(f"{FILES}{file_id}/approve-manager/", {}, format="json")
    assert second.status_code == 200, second.content
    assert second.json()["data"]["is_downloadable"] is False
    assert [row["id"] for row in learner.get(FILES).json()["data"]] == [file_id]

    # A view-only publication can be rendered/tracked but never receives a
    # learner download capability.
    assert learner.post(f"{FILES}{file_id}/track-view/", {}, format="json").status_code == 204
    download = learner.get(f"{FILES}{file_id}/download-url/")
    assert download.status_code == 409
    assert download.json()["code"] == "file_view_only"

    with schema_context(tenant_a.schema_name):
        stored = LessonFile.objects.get(pk=file_id)
        assert stored.uploaded_by_id == owner_user.pk
        assert stored.submission_audience == LessonFile.SubmissionAudience.GLOBAL


def test_explicit_teacher_audience_fails_closed_for_staff_principal(
    tenant_a,
    user_in,
    client_for,
    monkeypatch,
):
    _stub_upload(monkeypatch)
    staff, _staff_user, _ = _exact_actor(
        tenant=tenant_a,
        user_in=user_in,
        client_for=client_for,
        role=Role.LIBRARIAN,
    )
    with schema_context(tenant_a.schema_name):
        folder = FolderFactory(library=ContentLibraryFactory(visibility=ContentLibrary.Visibility.TENANT))
        before = LessonFile.objects.count()

    response = staff.post(
        UPLOAD,
        _upload_body(folder_id=folder.pk, audience="global"),
        format="json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "content_teacher_principal_required"
    with schema_context(tenant_a.schema_name):
        assert LessonFile.objects.count() == before


def test_upload_contract_is_closed_and_declares_audience_controls():
    from core.openapi import build_schema

    operation = build_schema(None)["paths"][UPLOAD]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["audience"]["enum"] == ["own_students", "global"]
    assert request_schema["properties"]["is_downloadable"]["type"] == "boolean"
    assert operation["security"] == [
        {"sessionAuth": []},
        {"cookieSession": [], "csrfHeader": []},
    ]


def test_database_rejects_an_ambiguous_lesson_and_folder_location(tenant_a):
    from apps.content.tests.factories import ContentLessonFactory, LessonFileFactory

    with schema_context(tenant_a.schema_name):
        file = LessonFileFactory()
        lesson = ContentLessonFactory()
        with pytest.raises(IntegrityError), transaction.atomic():
            LessonFile.objects.filter(pk=file.pk).update(lesson=lesson)
