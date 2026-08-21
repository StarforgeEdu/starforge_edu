"""Teacher directory filtering is strict, pre-pagination, and scope safe."""

from __future__ import annotations

from datetime import date

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db
URL = "/api/v1/teachers/"


def test_filters_are_exact_inclusive_and_applied_before_pagination(tenant_a, as_role):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        start = TeacherProfileFactory(
            branch=branch,
            is_active=True,
            subjects=["Directory filter algebra"],
            salary_type="monthly",
            hire_date=date(2026, 1, 10),
        )
        end = TeacherProfileFactory(
            branch=branch,
            is_active=True,
            subjects=["Directory filter algebra", "Physics"],
            salary_type="monthly",
            hire_date=date(2026, 1, 20),
        )
        TeacherProfileFactory(
            branch=branch,
            is_active=False,
            subjects=["Directory filter algebra"],
            salary_type="monthly",
            hire_date=date(2026, 1, 15),
        )
        TeacherProfileFactory(
            branch=branch,
            is_active=True,
            subjects=["Directory filter algebra advanced"],
            salary_type="monthly",
            hire_date=date(2026, 1, 15),
        )
        TeacherProfileFactory(
            branch=branch,
            is_active=True,
            subjects=["Directory filter algebra"],
            salary_type="hourly",
            hire_date=date(2026, 1, 15),
        )
        TeacherProfileFactory(
            branch=branch,
            is_active=True,
            subjects=["Directory filter algebra"],
            salary_type="monthly",
            hire_date=date(2026, 1, 9),
        )
        TeacherProfileFactory(
            branch=branch,
            is_active=True,
            subjects=["Directory filter algebra"],
            salary_type="monthly",
            hire_date=date(2026, 1, 21),
        )

    query = {
        "is_active": "true",
        "subject": "Directory filter algebra",
        "salary_type": "monthly",
        "hired_after": "2026-01-10",
        "hired_before": "2026-01-20",
        "ordering": "hire_date",
        "page_size": 1,
    }
    first = client.get(URL, query)
    assert first.status_code == 200, first.content
    assert first.json()["pagination"] == {
        "total": 2,
        "page": 1,
        "page_size": 1,
        "pages": 2,
        "has_next": True,
        "has_prev": False,
    }
    assert [row["id"] for row in first.json()["data"]] == [start.id]

    second = client.get(URL, {**query, "page": 2})
    assert second.status_code == 200, second.content
    assert second.json()["pagination"]["total"] == 2
    assert [row["id"] for row in second.json()["data"]] == [end.id]


def test_filters_cannot_widen_membership_scope_and_total_excludes_hidden_rows(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        visible_branch = BranchFactory()
        hidden_branch = BranchFactory()
        visible = TeacherProfileFactory(
            branch=visible_branch,
            subjects=["Scope sentinel subject"],
            hire_date=date(2026, 3, 1),
        )
        TeacherProfileFactory(
            branch=hidden_branch,
            subjects=["Scope sentinel subject"],
            hire_date=date(2026, 3, 1),
        )

    reader = user_in(tenant_a, roles=[Role.REGISTRAR], branch=visible_branch)
    client = as_user(tenant_a, reader)

    response = client.get(URL, {"subject": "Scope sentinel subject", "page_size": 1})
    assert response.status_code == 200, response.content
    assert response.json()["pagination"]["total"] == 1
    assert [row["id"] for row in response.json()["data"]] == [visible.id]

    cross_scope = client.get(
        URL,
        {"branch": hidden_branch.id, "subject": "Scope sentinel subject"},
    )
    assert cross_scope.status_code == 200, cross_scope.content
    assert cross_scope.json()["pagination"]["total"] == 0
    assert cross_scope.json()["data"] == []


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ({"is_active": "sometimes"}, "is_active"),
        ({"hired_after": "01-10-2026"}, "hired_after"),
        ({"hired_before": "2026-02-30"}, "hired_before"),
        ({"salary_type": "annual"}, "salary_type"),
        (
            {"hired_after": "2026-05-02", "hired_before": "2026-05-01"},
            "hired_before",
        ),
    ],
)
def test_invalid_filter_values_are_field_scoped_400(tenant_a, as_role, query, field):
    client, _ = as_role(Role.DIRECTOR)

    response = client.get(URL, query)

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"
    assert set(response.json()["errors"]) == {field}


def test_compensation_filter_is_a_non_oracular_permission_boundary(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.tests.factories import BranchFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
    reader = user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch)
    client = as_user(tenant_a, reader)

    known = client.get(URL, {"salary_type": "monthly"})
    unknown = client.get(URL, {"salary_type": "does-not-exist"})

    assert known.status_code == unknown.status_code == 403
    assert known.json() == unknown.json()
    assert known.json()["code"] == "forbidden"
    assert "salary_type" not in known.json().get("errors", {})


def test_compensation_visibility_intersects_teacher_and_compensation_membership_scopes(
    tenant_a,
    user_in,
    as_user,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        finance_branch = BranchFactory(name="Compensation-visible faculty")
        faculty_only_branch = BranchFactory(name="Faculty-only")
        viewer = user_in(tenant_a)
        faculty_type = AccountType.objects.create(
            name="Two-branch faculty reader",
            slug="two-branch-faculty-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=faculty_type,
            permission="teachers:read",
        )
        finance_type = AccountType.objects.create(
            name="Local compensation reader",
            slug="local-compensation-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=finance_type,
            permission="compensation:read",
        )
        for branch in (finance_branch, faculty_only_branch):
            RoleMembership.objects.create(
                user=viewer,
                account_type=faculty_type,
                role=faculty_type.compatibility_role,
                branch=branch,
            )
        RoleMembership.objects.create(
            user=viewer,
            account_type=finance_type,
            role=finance_type.compatibility_role,
            branch=finance_branch,
        )
        finance_visible = TeacherProfileFactory(
            branch=finance_branch,
            salary_type="monthly",
            rate="8000000",
        )
        faculty_only = TeacherProfileFactory(
            branch=faculty_only_branch,
            salary_type="monthly",
            rate="9000000",
        )
        viewer.refresh_from_db()

    client = as_user(tenant_a, viewer)
    response = client.get(URL, {"page_size": 100})
    assert response.status_code == 200, response.content
    rows = {row["id"]: row for row in response.json()["data"]}
    assert rows[finance_visible.pk]["rate"] == "8000000.00"
    assert "rate" not in rows[faculty_only.pk]
    assert "salary_type" not in rows[faculty_only.pk]

    filtered = client.get(URL, {"salary_type": "monthly", "page_size": 100})
    assert filtered.status_code == 200, filtered.content
    assert {row["id"] for row in filtered.json()["data"]} == {finance_visible.pk}
    assert filtered.json()["pagination"]["total"] == 1

    detail = client.get(f"{URL}{faculty_only.pk}/")
    assert detail.status_code == 200, detail.content
    assert "rate" not in detail.json()["data"]
    assert "salary_type" not in detail.json()["data"]
