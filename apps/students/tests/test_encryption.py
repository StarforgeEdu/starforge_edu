"""Student safeguarding fields are encrypted at rest and exactly role-scoped."""

from __future__ import annotations

import pytest
from django.db import connection
from django_tenants.utils import schema_context

from apps.org.tests.factories import BranchFactory
from apps.students.models import StudentProfile
from apps.students.services import create_student
from core.permissions import Role

pytestmark = pytest.mark.django_db

SECRET = "peanut allergy; carries epipen"
EMERGENCY_CONTACTS = [{"name": "Emergency guardian", "phone": "+998901234567", "relationship": "mother"}]


def test_student_safeguarding_fields_are_encrypted_at_rest(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = create_student(
            branch=branch,
            phone="+998905553001",
            medical_notes=SECRET,
            emergency_contacts=EMERGENCY_CONTACTS,
        )
        table = StudentProfile._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT medical_notes, emergency_contacts FROM {table} WHERE id = %s",  # nosec B608
                [student.pk],
            )
            raw_notes, raw_contacts = cursor.fetchone()
        assert raw_notes != SECRET
        assert raw_notes.startswith("gAAAA")  # Fernet token marker
        assert raw_contacts.startswith("gAAAA")
        assert "+998901234567" not in raw_contacts
        student.refresh_from_db()
        assert student.medical_notes == SECRET  # ORM round-trip decrypts
        assert student.emergency_contacts == EMERGENCY_CONTACTS


@pytest.fixture
def student_with_notes(tenant_a):
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
        student = create_student(
            branch=branch,
            phone="+998905553002",
            medical_notes=SECRET,
            emergency_contacts=EMERGENCY_CONTACTS,
        )
    return branch, student


def test_list_payload_has_no_medical_notes_key(tenant_a, user_in, as_user, student_with_notes):
    branch, _student = student_with_notes
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))
    body = client.get("/api/v1/students/").json()
    assert body["data"]
    assert all("medical_notes" not in row and "emergency_contacts" not in row for row in body["data"])


def test_teacher_retrieve_gets_null_medical_notes(tenant_a, user_in, as_user, student_with_notes):
    branch, student = student_with_notes
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    resp = client.get(f"/api/v1/students/{student.id}/")
    assert resp.status_code == 200
    assert resp.json()["data"]["medical_notes"] is None
    assert resp.json()["data"]["emergency_contacts"] is None


@pytest.mark.parametrize("role", [Role.REGISTRAR, Role.DIRECTOR])
def test_medical_roles_retrieve_plaintext(tenant_a, user_in, as_user, student_with_notes, role):
    branch, student = student_with_notes
    client = as_user(tenant_a, user_in(tenant_a, roles=[role], branch=branch))
    resp = client.get(f"/api/v1/students/{student.id}/")
    assert resp.status_code == 200
    assert resp.json()["data"]["medical_notes"] == SECRET
    assert resp.json()["data"]["emergency_contacts"] == EMERGENCY_CONTACTS


# --------------------------------------------------------------------------- #
# Update path must honour the separate safeguarding permission. A writer who is
# not a medical reader (head_of_dept has students:* but no safeguarding grant)
# must neither read nor replace medical_notes.
# --------------------------------------------------------------------------- #
def test_non_medical_writer_patch_does_not_leak_medical_notes(tenant_a, user_in, as_user, student_with_notes):
    branch, student = student_with_notes
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch))
    resp = client.patch(
        f"/api/v1/students/{student.id}/",
        {"academic_level": "grade-7"},
        format="json",
    )
    assert resp.status_code == 200
    # The write took effect...
    assert resp.json()["data"]["academic_level"] == "grade-7"
    # ...but the PHI is gated out of the response, same as retrieve.
    assert resp.json()["data"]["medical_notes"] is None
    assert resp.json()["data"]["emergency_contacts"] is None
    # And the gate is real: the value persisted, a medical reader still sees it.
    student.refresh_from_db()
    assert student.medical_notes == SECRET


def test_non_medical_writer_cannot_write_medical_notes(tenant_a, user_in, as_user, student_with_notes):
    """A broad students write grant is not a safeguarding write grant."""
    branch, student = student_with_notes
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch))
    resp = client.patch(
        f"/api/v1/students/{student.id}/",
        {"medical_notes": "updated: tree-nut allergy"},
        format="json",
    )
    assert resp.status_code == 403
    student.refresh_from_db()
    assert student.medical_notes == SECRET


@pytest.mark.parametrize("role", [Role.REGISTRAR, Role.DIRECTOR])
def test_medical_role_patch_still_sees_medical_notes(tenant_a, user_in, as_user, student_with_notes, role):
    branch, student = student_with_notes
    client = as_user(tenant_a, user_in(tenant_a, roles=[role], branch=branch))
    resp = client.patch(
        f"/api/v1/students/{student.id}/",
        {"academic_level": "grade-8"},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["medical_notes"] == SECRET


def test_safeguarding_grant_cannot_be_borrowed_across_membership_scopes(tenant_a, user_in, as_user):
    """Registrar in A + student manager in B must not expose Branch-B PHI."""
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory.create(name="Safeguarding A")
        branch_b = BranchFactory.create(name="Students B")
        student_b = create_student(
            branch=branch_b,
            phone="+998905553099",
            medical_notes="branch-b private note",
            emergency_contacts=[{"name": "Branch B", "phone": "+998909999999"}],
        )
    user = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch_a)
    with schema_context(tenant_a.schema_name):
        RoleMembership.objects.create(user=user, branch=branch_b, role=Role.HEAD_OF_DEPT)
        user.refresh_from_db()

    client = as_user(tenant_a, user)
    response = client.get(f"/api/v1/students/{student_b.pk}/")

    assert response.status_code == 200
    assert response.json()["data"]["medical_notes"] is None
    assert response.json()["data"]["emergency_contacts"] is None

    write = client.patch(
        f"/api/v1/students/{student_b.pk}/",
        {"medical_notes": "cross-scope replacement"},
        format="json",
    )
    assert write.status_code == 403
    student_b.refresh_from_db()
    assert student_b.medical_notes == "branch-b private note"
    assert student_b.emergency_contacts == [{"name": "Branch B", "phone": "+998909999999"}]


def test_corrupt_emergency_contacts_fail_closed_only_after_authorized_projection(
    tenant_a,
    user_in,
    as_user,
    student_with_notes,
):
    branch, student = student_with_notes
    with schema_context(tenant_a.schema_name), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {StudentProfile._meta.db_table} SET emergency_contacts = %s WHERE id = %s",  # nosec B608
            ["corrupt-emergency-contact-token", student.pk],
        )

    directory = as_user(tenant_a, user_in(tenant_a, roles=[Role.TEACHER], branch=branch))
    hidden = directory.get(f"/api/v1/students/{student.pk}/")
    assert hidden.status_code == 200, hidden.content
    assert hidden.json()["data"]["emergency_contacts"] is None

    safeguarding = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch),
    )
    safeguarding.raise_request_exception = False
    failure = safeguarding.get(f"/api/v1/students/{student.pk}/")
    assert failure.status_code == 500
    assert b"corrupt-emergency-contact-token" not in failure.content


def test_students_writer_cannot_set_safeguarding_fields_on_create(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory.create()
    client = as_user(
        tenant_a,
        user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch),
    )

    response = client.post(
        "/api/v1/students/",
        {
            "branch": branch.pk,
            "phone": "+998905553098",
            "medical_notes": "must not be accepted",
            "emergency_contacts": [{"name": "Private", "phone": "+998901111111"}],
        },
        format="json",
    )

    assert response.status_code == 403
    with schema_context(tenant_a.schema_name):
        assert not StudentProfile.objects.filter(phone="+998905553098").exists()
