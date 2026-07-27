"""Audit-driven input hardening for the off-DRF migrated endpoints.

The DRF serializer layer used to reject over-long / out-of-range / malformed input
before it reached the DB. The plain views lost that, so these pin the replacements:
a defensive DataError/IntegrityError -> 4xx mapper, FK-filter + page-overflow + NUL
guards in core.listing/http, decimal finiteness, email format, and the read-only
impersonation write-deny — none of these inputs may 500.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.exceptions import ValidationException
from core.http import bool_field
from core.permissions import Role

pytestmark = pytest.mark.django_db


@pytest.fixture
def director(as_role):
    return as_role(Role.DIRECTOR)[0]


# --- core.listing / core.http never-500 -----------------------------------
@pytest.mark.parametrize("bad", [None, "", "banana", "treu", [], {}])
def test_bool_field_rejects_ambiguous_values(bad):
    with pytest.raises(ValidationException):
        bool_field({"flag": bad}, "flag")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("on", True), ("yes", True), ("off", False), ("no", False)],
)
def test_bool_field_accepts_explicit_boolean_forms(raw, expected):
    assert bool_field({"flag": raw}, "flag") is expected


@pytest.mark.parametrize("bad", [None, "banana"])
def test_card_type_is_active_rejects_null_and_garbage(director, bad):
    response = director.post(
        "/api/v1/cards/types/", {"name": "Strict bool", "is_active": bad}, format="json"
    )
    assert response.status_code == 400
    assert "is_active" in response.json()["errors"]


def test_academics_subject_patch_rejects_boolean_typo(director, tenant_a):
    from apps.academics.tests.factories import SubjectFactory

    with schema_context(tenant_a.schema_name):
        subject = SubjectFactory(is_active=True)
    response = director.patch(
        f"/api/v1/academics/subjects/{subject.pk}/", {"is_active": "treu"}, format="json"
    )
    assert response.status_code == 400
    assert "is_active" in response.json()["errors"]


@pytest.mark.parametrize(
    ("url", "model_path", "create_kwargs"),
    [
        ("/api/v1/content/libraries/{pk}/", "content.ContentLibrary", {}),
        (
            "/api/v1/schedule/lesson-types/{pk}/",
            "schedule.LessonType",
            {"slug": "strict-boolean"},
        ),
    ],
)
def test_app_local_boolean_patch_rejects_typo(
    director, tenant_a, url, model_path, create_kwargs
):
    from django.apps import apps

    model = apps.get_model(model_path)
    with schema_context(tenant_a.schema_name):
        instance = model.objects.create(name="Strict boolean", **create_kwargs)
    response = director.patch(url.format(pk=instance.pk), {"is_active": "fasle"}, format="json")
    assert response.status_code == 400
    assert "is_active" in response.json()["errors"]


def test_fk_filter_garbage_is_400_not_500(director):
    resp = director.get("/api/v1/teachers/?branch=abc")
    assert resp.status_code == 400
    assert resp.json()["code"] == "validation_error"


def test_giant_page_returns_empty_not_500(director, tenant_a):
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create_batch(2)
    resp = director.get("/api/v1/students/?page=999999999999999999")
    assert resp.status_code == 200
    assert resp.json()["data"] == []  # past-the-end, not a bigint-OFFSET 500


def test_nul_byte_in_search_is_400_not_500(director):
    resp = director.get("/api/v1/students/?search=%00")
    assert resp.status_code == 400


# --- DataError safety net (over-length reaches the column) -----------------
def test_over_length_string_is_4xx_not_500(director, tenant_a):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        student = StudentProfileFactory.create(branch=branch)
    # academic_level is CharField(64); >64 chars would DataError-500 without the map.
    resp = director.patch(
        f"/api/v1/students/{student.id}/", {"academic_level": "x" * 200}, format="json"
    )
    assert resp.status_code in (400, 422), resp.content
    assert resp.status_code != 500


# --- decimal finiteness (silent NaN corruption / overflow 500) -------------
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_teacher_rate_rejects_non_finite(director, tenant_a, bad):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = director.post(
        "/api/v1/teachers/",
        {"branch": branch.id, "phone": "+998905550001", "rate": bad},
        format="json",
    )
    assert resp.status_code == 400
    assert "rate" in resp.json()["errors"]


def test_teacher_salary_type_invalid_choice_is_400(director, tenant_a):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = director.post(
        "/api/v1/teachers/",
        {"branch": branch.id, "phone": "+998905550002", "salary_type": "weekly"},
        format="json",
    )
    assert resp.status_code == 400
    assert "salary_type" in resp.json()["errors"]


def test_department_budget_nan_is_400(director, tenant_a):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = director.post(
        "/api/v1/org/departments/",
        {"branch": branch.id, "name": "Math", "slug": "math", "budget": "NaN"},
        format="json",
    )
    assert resp.status_code == 400


# --- email format (stored as the login identifier) -------------------------
def test_student_create_rejects_garbage_email(director, tenant_a):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    resp = director.post(
        "/api/v1/students/", {"branch": branch.id, "email": "not-an-email"}, format="json"
    )
    assert resp.status_code == 400
    assert "email" in resp.json()["errors"]


# --- read-only impersonation write-deny ------------------------------------
def test_read_only_session_cannot_logout_or_change_password(tenant_a, user_in, client_for):
    from core.session_auth import create_session

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        session = create_session(user, read_only=True)  # an impersonation session
    client = client_for(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {session.key}")

    logout = client.post("/api/v1/auth/logout/", {}, format="json")
    assert logout.status_code == 403
    assert logout.json()["code"] == "read_only_token"

    change = client.post(
        "/api/v1/auth/password/change/",
        {"old_password": "x", "new_password": "y"},
        format="json",
    )
    assert change.status_code == 403
    assert change.json()["code"] == "read_only_token"


# --- branch-reassignment scope on update -----------------------------------
def test_patch_cannot_move_cohort_to_out_of_scope_branch(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory()
        branch_b = BranchFactory()
        cohort = CohortFactory(branch=branch_a)
    # A registrar scoped to branch_a holds cohorts:write but must not reassign the row
    # into branch_b (outside their memberships).
    client = as_user(tenant_a, user_in(tenant_a, roles=["registrar"], branch=branch_a))
    resp = client.patch(
        f"/api/v1/cohorts/{cohort.id}/", {"branch": branch_b.id}, format="json"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "out_of_scope"


def test_csv_import_into_out_of_scope_branch_is_403(tenant_a, user_in, as_user):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        mine = BranchFactory()
        theirs = BranchFactory()
    client = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=mine))
    upload = SimpleUploadedFile("s.csv", b"phone\n+998905559999\n", content_type="text/csv")
    resp = client.post(
        "/api/v1/students/import/", {"file": upload, "branch": theirs.id}, format="multipart"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "out_of_scope"
