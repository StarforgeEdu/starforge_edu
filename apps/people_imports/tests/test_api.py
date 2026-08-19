from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.people_imports.models import PeopleImportDraft
from apps.students.models import StudentProfile
from apps.teachers.models import TeacherProfile
from core.permissions import Role
from tests.role_principal_helpers import ensure_role_principal, exact_session_client

pytestmark = pytest.mark.django_db

URL = "/api/v1/people-imports/"


@pytest.fixture
def director(tenant_a, user_in, client_for):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create(name="Central Campus", slug="central-campus")
        user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
        ensure_role_principal(user, roles=[Role.DIRECTOR], branch=branch)
    return exact_session_client(client_for, tenant_a, user), branch


def _upload(client, branch, csv_text):
    file_obj = SimpleUploadedFile("students.csv", csv_text.encode(), content_type="text/csv")
    return client.post(
        URL,
        {"kind": "student", "default_branch": branch.pk, "file": file_obj},
        format="multipart",
    )


def test_upload_is_a_reviewable_draft_and_does_not_create_accounts(director, tenant_a):
    client, branch = director

    response = _upload(client, branch, "First name,Last name,Email\nAziza,Karimova,aziza@example.test\n")

    assert response.status_code == 201
    draft = response.json()["data"]
    assert draft["status"] == "draft"
    assert draft["ready_count"] == 1
    assert draft["can_confirm"] is True
    with schema_context(tenant_a.schema_name):
        assert StudentProfile.objects.count() == 0
        assert PeopleImportDraft.objects.get(pk=draft["id"]).source_file_name == "students.csv"

    listed = client.get(URL, {"kind": "student", "status": "active"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [draft["id"]]


def test_invalid_row_can_be_corrected_then_confirmed_by_one_background_job(director, tenant_a):
    client, branch = director
    uploaded = _upload(client, branch, "First name,Last name\nAziza,Karimova\n")
    draft = uploaded.json()["data"]
    assert draft["error_count"] == 1

    row_response = client.get(f"{URL}{draft['id']}/rows/", {"state": "invalid"})
    assert row_response.status_code == 200
    row = row_response.json()["data"][0]
    assert "phone" in row["errors"]

    corrected = {**row["data"], "email": "aziza@example.test"}
    saved = client.patch(
        f"{URL}{draft['id']}/",
        {"rows": [{"id": row["id"], "data": corrected, "is_included": True}]},
        format="json",
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["error_count"] == 0
    assert saved.json()["data"]["ready_count"] == 1

    confirmed = client.post(
        f"{URL}{draft['id']}/confirm/",
        {"confirmed": True},
        format="json",
    )
    assert confirmed.status_code == 202
    assert confirmed.json()["data"]["status"] == "completed"
    assert confirmed.json()["data"]["imported_count"] == 1
    with schema_context(tenant_a.schema_name):
        student = StudentProfile.objects.get(email="aziza@example.test")
        assert student.branch_id == branch.pk


def test_confirmation_requires_explicit_warning_acknowledgement(director):
    client, branch = director
    draft = _upload(client, branch, "First name,Email\nAziza,aziza@example.test\n").json()["data"]

    response = client.post(f"{URL}{draft['id']}/confirm/", {"confirmed": False}, format="json")

    assert response.status_code == 400
    assert response.json()["code"] == "confirmation_required"


def test_teacher_workbook_rows_use_the_same_review_and_bounded_confirmation_flow(director, tenant_a):
    client, branch = director
    upload = SimpleUploadedFile(
        "teachers.csv",
        b"First name,Last name,Email,Subjects,Hire date\nDilshod,Rahimov,dilshod@example.test,English; Speaking,2026-08-01\n",
        content_type="text/csv",
    )

    response = client.post(
        URL,
        {"kind": "teacher", "default_branch": branch.pk, "file": upload},
        format="multipart",
    )
    assert response.status_code == 201
    draft = response.json()["data"]
    assert draft["ready_count"] == 1
    with schema_context(tenant_a.schema_name):
        assert TeacherProfile.objects.count() == 0

    confirmed = client.post(
        f"{URL}{draft['id']}/confirm/",
        {"confirmed": True},
        format="json",
    )
    assert confirmed.status_code == 202
    assert confirmed.json()["data"]["status"] == "completed"
    with schema_context(tenant_a.schema_name):
        teacher = TeacherProfile.objects.get(email="dilshod@example.test")
        assert teacher.branch_id == branch.pk
        assert teacher.subjects == ["English", "Speaking"]
