"""Permission, contract, privacy, and query-bound regressions for leadership profiles."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db


def _url(student_id: int, query: str = "") -> str:
    suffix = f"?{query}" if query else ""
    return f"/api/v1/students/{student_id}/leadership-profile/{suffix}"


def _student_fixture(tenant):
    from apps.academics.tests.factories import ExamFactory, ExamResultFactory, GradeFactory, SubjectFactory
    from apps.attendance.models import AttendanceRecord
    from apps.attendance.tests.factories import AttendanceRecordFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.tests.factories import DiscountFactory, InvoiceFactory
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory(name="Leadership Branch")
        department = DepartmentFactory(branch=branch, name="Leadership Department")
        teacher = TeacherProfileFactory(branch=branch, department=department)
        cohort = CohortFactory(
            branch=branch,
            department=department,
            primary_teacher=teacher,
        )
        student = StudentProfileFactory(
            branch=branch,
            current_cohort=cohort,
            first_name="Learner",
            last_name="One",
        )
        # A legacy unscoped key is intentionally present. It must never become a
        # signed/public URL merely because the aggregate was requested.
        student.photo.name = "students/photos/legacy-secret.jpg"
        student.save(update_fields=["photo"])

        term = TermFactory(
            start_date=timezone.localdate() - timedelta(days=120), end_date=timezone.localdate()
        )
        subject = SubjectFactory(department=department)
        exam = ExamFactory(
            cohort=cohort,
            term=term,
            subject=subject,
            exam_date=timezone.localdate() - timedelta(days=2),
            is_published=True,
        )
        ExamResultFactory(exam=exam, student=student)
        GradeFactory(
            student=student,
            subject=subject,
            term=term,
            is_published=True,
            is_valid=True,
        )
        lesson = Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="Leadership sample lesson",
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() - timedelta(days=1) + timedelta(hours=1),
            status=Lesson.Status.COMPLETED,
        )
        AttendanceRecordFactory(
            student=student,
            lesson=lesson,
            status=AttendanceRecord.Status.PRESENT,
        )
        parent = ParentProfileFactory(first_name="Guardian", last_name="One", phone="+998901234567")
        GuardianFactory(student=student, parent=parent, is_primary=True)
        InvoiceFactory(
            student=student,
            cohort=cohort,
            branch_at_issue=branch,
            department_at_issue=department,
            issue_date=timezone.localdate() - timedelta(days=5),
        )
        DiscountFactory(student=student, valid_from=None, valid_until=None)
        return branch, department, student


def test_director_profile_is_truthful_permission_pruned_and_never_exposes_photo_key(
    tenant_a,
    user_in,
    as_user,
):
    branch, _department, student = _student_fixture(tenant_a)
    director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)

    response = as_user(tenant_a, director).get(_url(student.pk))

    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert body["identity"]["public_student_id"] == student.student_id
    assert body["identity"]["branch"] == {"id": branch.pk, "name": branch.name}
    assert body["identity"]["photo"] == {"available": True, "download_url": None}
    assert "legacy-secret" not in response.content.decode()
    assert body["learning"]["recent_exam_results"][0]["score_fraction"] == 0.8
    assert body["attendance"]["attendance_rate_fraction"] == 1.0
    assert body["attendance"]["countable_sessions"] == 1
    assert body["family"]["guardians"][0]["contacts"]["verification_status"] == "not_recorded"
    assert body["finance"]["window"]["billed"]["currency"] == "UZS"
    assert isinstance(body["finance"]["window"]["billed"]["amount_minor"], int)
    assert body["coverage"]["finance"]["status"] == "available"
    assert {warning["code"] for warning in body["warnings"]} >= {
        "student_photo_unavailable",
        "record_actor_not_recorded",
    }


def test_permission_from_another_branch_cannot_be_borrowed_for_finance_section(
    tenant_a,
    user_in,
    as_user,
):
    from apps.access.models import AccountType, AccountTypePermission
    from apps.org.tests.factories import BranchFactory
    from apps.users.models import RoleMembership

    own_branch, _department, student = _student_fixture(tenant_a)
    operator = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        foreign_branch = BranchFactory(name="Foreign finance branch")
        student_reader = AccountType.objects.create(
            name="Student profile reader",
            slug="student-profile-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        finance_reader = AccountType.objects.create(
            name="Foreign finance reader",
            slug="foreign-finance-reader",
            account_kind=AccountType.AccountKind.STAFF,
        )
        AccountTypePermission.objects.create(
            account_type=student_reader,
            permission="students:read",
        )
        AccountTypePermission.objects.create(
            account_type=finance_reader,
            permission="finance:read",
        )
        RoleMembership.objects.create(
            user=operator,
            branch=own_branch,
            role=Role.SUPPORT,
            account_type=student_reader,
        )
        RoleMembership.objects.create(
            user=operator,
            branch=foreign_branch,
            role=Role.SUPPORT,
            account_type=finance_reader,
        )
        operator.refresh_from_db()

    response = as_user(tenant_a, operator).get(_url(student.pk))

    assert response.status_code == 200, response.content
    body = response.json()["data"]
    assert "finance" not in body
    assert body["coverage"]["finance"]["status"] == "not_authorized"


def test_department_scope_returns_not_found_for_sibling_student(tenant_a, user_in, as_user):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership

    manager = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        own_department = DepartmentFactory(branch=branch)
        sibling_department = DepartmentFactory(branch=branch)
        sibling_group = CohortFactory(branch=branch, department=sibling_department)
        sibling = StudentProfileFactory(branch=branch, current_cohort=sibling_group)
        RoleMembership.objects.create(
            user=manager,
            branch=branch,
            department=own_department,
            role=Role.HEAD_OF_DEPT,
        )
        manager.refresh_from_db()

    response = as_user(tenant_a, manager).get(_url(sibling.pk))

    assert response.status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        "from=2026-01-01",
        "date_from=2026-01-01&date_from=2026-01-02",
        "date_from=not-a-date",
        "date_from=2026-02-01&date_to=2026-01-01",
        "date_from=2024-01-01&date_to=2026-01-01",
    ],
)
def test_profile_rejects_unknown_duplicate_malformed_reversed_and_unbounded_windows(
    tenant_a,
    user_in,
    as_user,
    query,
):
    branch, _department, student = _student_fixture(tenant_a)
    director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)

    response = as_user(tenant_a, director).get(_url(student.pk, query))

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"


def test_profile_query_count_is_bounded_for_multiple_related_rows(
    tenant_a,
    user_in,
    as_user,
):
    from django.db import connection

    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory

    branch, _department, student = _student_fixture(tenant_a)
    with schema_context(tenant_a.schema_name):
        for index in range(5):
            GuardianFactory(
                student=student,
                parent=ParentProfileFactory(phone=f"+99890999{index:04d}"),
                is_primary=False,
            )
    director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
    client = as_user(tenant_a, director)

    # The exact budget is deliberately generous enough to include middleware,
    # live permission resolution, and all six optional panels while still
    # catching row-count-dependent presenter queries.
    with CaptureQueriesContext(connection) as queries:
        response = client.get(_url(student.pk))

    assert response.status_code == 200, response.content
    assert len(queries) <= 45, [query["sql"] for query in queries]
