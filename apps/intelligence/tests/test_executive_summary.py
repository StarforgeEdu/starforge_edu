"""Permission, scope, cache, and performance contracts for the executive snapshot."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.access.models import AccountType, AccountTypePermission
from core.historical_scope import ScopeAttributionStatus
from core.permissions import Role

pytestmark = pytest.mark.django_db

SUMMARY = "/api/v1/intelligence/executive-summary/"


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return timezone.make_aware(
        datetime(year, month, day, hour),
        timezone.get_current_timezone(),
    )


def _custom_type(*, slug: str, permissions: set[str]) -> AccountType:
    account_type = AccountType.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        account_kind=AccountType.AccountKind.STAFF,
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in sorted(permissions)
        ]
    )
    return account_type


def _branch_fixture(*, branch_slug: str, department_slug: str, student_count: int = 2):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import BranchFactory, DepartmentFactory
    from apps.students.tests.factories import StudentProfileFactory

    branch = BranchFactory(slug=branch_slug, name=branch_slug.replace("-", " ").title())
    department = DepartmentFactory(
        branch=branch,
        slug=department_slug,
        name=department_slug.replace("-", " ").title(),
    )
    cohort = CohortFactory(branch=branch, department=department, name=f"{branch_slug} cohort")
    students = [
        StudentProfileFactory(
            branch=branch,
            current_cohort=cohort,
            status=("active" if index else "lead"),
            enrollment_date=date(2026, 8, 1),
        )
        for index in range(student_count)
    ]
    return branch, department, cohort, students


def _attendance(*, branch, department, cohort, students) -> None:
    from apps.attendance.models import AttendanceRecord
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    teacher = TeacherProfileFactory(branch=branch, department=department)
    starts_at = _at(2026, 8, 2, 10)
    lesson = Lesson.objects.create(
        term=TermFactory(),
        cohort=cohort,
        teacher=teacher,
        title="Executive snapshot lesson",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
    )
    AttendanceRecord.objects.create(
        student=students[0],
        lesson=lesson,
        status=AttendanceRecord.Status.PRESENT,
    )
    AttendanceRecord.objects.create(
        student=students[1],
        lesson=lesson,
        status=AttendanceRecord.Status.ABSENT,
    )


def _finance(*, branch, student) -> None:
    from apps.finance.models import Expense, Invoice, PaymentAllocation, Refund
    from apps.finance.tests.factories import InvoiceFactory
    from apps.payments.models import Payment

    invoice = InvoiceFactory(
        number=f"INV-EXEC-{branch.pk}",
        student=student,
        issue_date=date(2026, 8, 1),
        status=Invoice.Status.OVERDUE,
        total_uzs=Decimal("15000.00"),
    )
    payment = Payment.objects.create(
        provider=Payment.Method.CASH,
        amount_uzs=Decimal("5000.00"),
        status=Payment.Status.COMPLETED,
        allocation_status=Payment.Allocation.ALLOCATED,
        idempotency_key=f"executive-payment-{branch.pk}-{student.pk}",
        account_ref=invoice.number,
        paid_at=_at(2026, 8, 2),
        branch_at_payment=branch,
        department_at_payment=(
            student.current_cohort.department if student.current_cohort is not None else None
        ),
        attribution_status=ScopeAttributionStatus.CAPTURED,
        metadata={"invoice_id": invoice.pk, "student_id": student.pk},
    )
    PaymentAllocation.objects.create(
        invoice=invoice,
        payment_id=payment.pk,
        amount_uzs=Decimal("5000.00"),
    )
    Refund.objects.create(
        invoice=invoice,
        payment_id=payment.pk,
        amount_uzs=Decimal("1000.00"),
        state=Refund.State.COMPLETED,
        provider_confirmed_at=_at(2026, 8, 2),
    )
    Expense.objects.create(
        branch=branch,
        description="Approved supplies",
        amount_uzs=Decimal("2000.00"),
        status=Expense.Status.APPROVED,
        approved_at=_at(2026, 8, 2),
    )


def _window(branch_id: int) -> dict[str, str | int]:
    return {
        "branch": branch_id,
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
    }


def test_director_contract_uses_one_scope_window_and_permission_pruned_aggregates(
    tenant_a,
    user_in,
    as_user,
):
    from apps.org.models import CenterSettings

    with schema_context(tenant_a.schema_name):
        branch, department, cohort, students = _branch_fixture(
            branch_slug="executive-central",
            department_slug="executive-languages",
        )
        remote, _remote_department, _remote_cohort, remote_students = _branch_fixture(
            branch_slug="executive-remote",
            department_slug="executive-sciences",
            student_count=1,
        )
        _attendance(
            branch=branch,
            department=department,
            cohort=cohort,
            students=students,
        )
        _finance(branch=branch, student=students[1])
        _finance(branch=remote, student=remote_students[0])
        settings_row = CenterSettings.load()
        settings_row.currency_primary = "EUR"
        settings_row.save(update_fields=("currency_primary",))
        director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)

    response = as_user(tenant_a, director).get(SUMMARY, _window(branch.pk))

    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "private, no-cache, max-age=0, must-revalidate"
    assert {"accept-language", "authorization"} <= {
        item.strip().lower() for item in response["Vary"].split(",")
    }
    assert response["ETag"].startswith('"')
    data = response.json()["data"]
    assert data["currency"] == "UZS"
    assert data["window"] == {
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
        "timezone": "Asia/Tashkent",
        "inclusive": "both",
    }
    assert data["scope"]["branches"] == [{"id": branch.pk, "name": branch.name}]
    assert data["scope"]["departments"] == [
        {"id": department.pk, "name": department.name, "branch": branch.pk}
    ]
    assert data["scope"]["applied_filters"] == {"branch": branch.pk, "department": None}
    assert data["students"] == {
        "total": 2,
        "active": 1,
        "leads": 1,
        "graduated": 0,
        "withdrawn": 0,
        "blocked": 0,
        "with_cohort": 2,
        "ungrouped": 0,
        "joined_in_window": 2,
    }
    assert data["attendance"] == {
        "attended": 1,
        "absent": 1,
        "excused": 0,
        "denominator": 2,
        "attendance_rate_fraction": 0.5,
    }
    assert data["finance"] == {
        "billed": {"amount_minor": 1_500_000, "currency": "UZS"},
        "collected": {"amount_minor": 500_000, "currency": "UZS"},
        "outstanding_for_invoices_issued_in_window": {
            "amount_minor": 1_000_000,
            "currency": "UZS",
        },
        "overdue_invoice_count": 1,
        "refunded": {"amount_minor": 100_000, "currency": "UZS"},
        "approved_expense": {"amount_minor": 200_000, "currency": "UZS"},
        "paid_expense": {"amount_minor": 0, "currency": "UZS"},
    }
    assert data["retention"] == {
        "current_student_sample_size": 2,
        "joined_students": 2,
        "exited_students": 0,
        "exit_events": 0,
        "attribution": "current_student_scope",
    }
    assert data["capacity"] == {
        "active_group_count": 1,
        "groups_with_declared_capacity": 0,
        "groups_without_declared_capacity": 1,
        "declared_seats": 0,
        "active_students": 1,
        "active_students_in_measured_groups": 0,
        "seat_balance": 0,
        "attribution": "current_group_scope",
    }
    assert data["risk"] == {
        "student_sample_size": 1,
        "at_risk_students": 1,
        "high_risk_students": 0,
        "medium_risk_students": 0,
        "low_risk_students": 1,
        "low_attendance_students": 0,
        "low_grade_students": 0,
        "overdue_payment_students": 1,
        "at_risk_rate_fraction": 1.0,
        "included_signals": ["low_attendance", "low_grades", "overdue_payment"],
        "finance_signal_included": True,
    }
    assert data["teachers"] == {
        "teacher_count": 1,
        "active_teacher_count": 1,
        "completed_lessons": 0,
        "teachers_delivering": 0,
        "groups_delivered": 0,
        "attendance_numerator": 1,
        "attendance_denominator": 2,
        "students_reached": 2,
        "lessons_with_attendance": 1,
        "published_exams_with_results": 0,
        "graded_results": 0,
        "assessed_students": 0,
        "published_exams": 0,
        "attendance_rate_fraction": 0.5,
    }
    assert data["attention"] == {
        "tasks": {
            "open_assigned_to_me": 0,
            "blocked_assigned_to_me": 0,
            "overdue_assigned_to_me": 0,
        },
        "pending_approvals": 0,
        "upcoming_meetings": 0,
    }
    assert data["coverage"]["finance"]["status"] == "complete"
    assert data["coverage"]["finance"]["attribution"] == "immutable_historical_scope"
    assert data["coverage"]["notifications"]["reason"] == "scope_not_representable"
    assert data["warnings"] == [
        {
            "code": "scope_not_representable",
            "message": "Unread notifications are unavailable for a filtered organization scope.",
            "affected_sections": ["notifications"],
        }
    ]
    assert data["branches"] == [
        {
            "id": branch.pk,
            "name": branch.name,
            "student_count": 2,
            "attendance_numerator": 1,
            "attendance_denominator": 2,
            "attendance_rate_fraction": 0.5,
        }
    ]


def test_student_branch_transfer_does_not_move_historical_finance_totals(
    tenant_a,
    user_in,
    as_user,
):
    with schema_context(tenant_a.schema_name):
        source, _source_department, _source_cohort, source_students = _branch_fixture(
            branch_slug="historical-finance-source",
            department_slug="historical-finance-source-department",
            student_count=1,
        )
        destination, _destination_department, destination_cohort, _destination_students = _branch_fixture(
            branch_slug="historical-finance-destination",
            department_slug="historical-finance-destination-department",
            student_count=1,
        )
        student = source_students[0]
        _finance(branch=source, student=student)

        student.branch = destination
        student.current_cohort = destination_cohort
        student.save(update_fields=("branch", "current_cohort", "updated_at"))
        director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=source)

    cache.clear()
    client = as_user(tenant_a, director)
    source_response = client.get(SUMMARY, _window(source.pk))
    destination_response = client.get(SUMMARY, _window(destination.pk))
    assert source_response.status_code == 200, source_response.content
    assert destination_response.status_code == 200, destination_response.content
    source_data = source_response.json()["data"]
    destination_data = destination_response.json()["data"]

    assert source_data["finance"]["billed"]["amount_minor"] == 1_500_000
    assert source_data["finance"]["collected"]["amount_minor"] == 500_000
    assert source_data["finance"]["outstanding_for_invoices_issued_in_window"]["amount_minor"] == 1_000_000
    assert source_data["finance"]["refunded"]["amount_minor"] == 100_000
    assert source_data["coverage"]["finance"]["attribution"] == "immutable_historical_scope"

    assert destination_data["finance"]["billed"]["amount_minor"] == 0
    assert destination_data["finance"]["collected"]["amount_minor"] == 0
    assert destination_data["finance"]["outstanding_for_invoices_issued_in_window"]["amount_minor"] == 0
    assert destination_data["finance"]["refunded"]["amount_minor"] == 0


def test_student_department_transfer_does_not_move_historical_finance_totals(
    tenant_a,
    user_in,
    as_user,
):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import DepartmentFactory

    with schema_context(tenant_a.schema_name):
        branch, source_department, _source_cohort, students = _branch_fixture(
            branch_slug="historical-department-branch",
            department_slug="historical-department-source",
            student_count=1,
        )
        destination_department = DepartmentFactory(
            branch=branch,
            slug="historical-department-destination",
            name="Historical Department Destination",
        )
        destination_cohort = CohortFactory(
            branch=branch,
            department=destination_department,
            name="Historical destination cohort",
        )
        student = students[0]
        _finance(branch=branch, student=student)

        student.current_cohort = destination_cohort
        student.save(update_fields=("current_cohort", "updated_at"))
        director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)

    cache.clear()
    client = as_user(tenant_a, director)
    source_response = client.get(
        SUMMARY,
        {**_window(branch.pk), "department": source_department.pk},
    )
    destination_response = client.get(
        SUMMARY,
        {**_window(branch.pk), "department": destination_department.pk},
    )
    assert source_response.status_code == 200, source_response.content
    assert destination_response.status_code == 200, destination_response.content
    source_data = source_response.json()["data"]
    destination_data = destination_response.json()["data"]

    assert source_data["finance"]["billed"]["amount_minor"] == 1_500_000
    assert source_data["finance"]["collected"]["amount_minor"] == 500_000
    assert destination_data["finance"]["billed"]["amount_minor"] == 0
    assert destination_data["finance"]["collected"]["amount_minor"] == 0


@pytest.mark.parametrize(
    "attribution_status",
    [
        ScopeAttributionStatus.UNRESOLVED,
        ScopeAttributionStatus.CONFLICTING,
        ScopeAttributionStatus.QUARANTINED,
    ],
)
def test_executive_finance_omits_unreviewed_historical_rows(
    tenant_a,
    user_in,
    as_user,
    attribution_status,
):
    from apps.finance.models import Invoice, PaymentAllocation, Refund
    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        branch, _department, _cohort, students = _branch_fixture(
            branch_slug=f"unreviewed-finance-{attribution_status}",
            department_slug=f"unreviewed-finance-{attribution_status}-department",
            student_count=1,
        )
        student = students[0]
        invoice = Invoice.objects.create(
            number=f"INV-UNREVIEWED-{attribution_status}",
            student=student,
            branch_at_issue=None,
            department_at_issue=None,
            attribution_status=attribution_status,
            status=Invoice.Status.OVERDUE,
            issue_date=date(2026, 8, 1),
            due_date=date(2026, 8, 5),
            total_uzs=Decimal("15000.00"),
        )
        payment = Payment.objects.create(
            provider=Payment.Method.CASH,
            amount_uzs=Decimal("5000.00"),
            status=Payment.Status.COMPLETED,
            allocation_status=Payment.Allocation.ALLOCATED,
            idempotency_key=f"unreviewed-payment-{attribution_status}",
            account_ref=invoice.number,
            paid_at=_at(2026, 8, 2),
            branch_at_payment=None,
            department_at_payment=None,
            attribution_status=attribution_status,
        )
        PaymentAllocation.objects.create(
            invoice=invoice,
            payment_id=payment.pk,
            amount_uzs=Decimal("5000.00"),
        )
        Refund.objects.create(
            invoice=invoice,
            payment_id=payment.pk,
            amount_uzs=Decimal("1000.00"),
            state=Refund.State.COMPLETED,
            provider_confirmed_at=_at(2026, 8, 2),
        )
        director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)

    cache.clear()
    response = as_user(tenant_a, director).get(SUMMARY, _window(branch.pk))
    assert response.status_code == 200, response.content
    data = response.json()["data"]

    assert data["finance"]["billed"]["amount_minor"] == 0
    assert data["finance"]["collected"]["amount_minor"] == 0
    assert data["finance"]["outstanding_for_invoices_issued_in_window"]["amount_minor"] == 0
    assert data["finance"]["overdue_invoice_count"] == 0
    assert data["finance"]["refunded"]["amount_minor"] == 0
    assert data["coverage"]["finance"]["attribution"] == "immutable_historical_scope"


def test_department_head_is_exactly_scoped_and_finance_is_omitted_not_zero(
    tenant_a,
    user_in,
    as_user,
):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.org.tests.factories import DepartmentFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch, department, _cohort, _students = _branch_fixture(
            branch_slug="hod-local",
            department_slug="hod-local-department",
        )
        other_department = DepartmentFactory(
            branch=branch,
            slug="hod-same-branch-other-department",
        )
        other_cohort = CohortFactory(
            branch=branch,
            department=other_department,
            name="HOD same-branch other cohort",
        )
        StudentProfileFactory(branch=branch, current_cohort=other_cohort)
        user = user_in(tenant_a)
        RoleMembership.objects.create(
            user=user,
            branch=branch,
            department=department,
            role=Role.HEAD_OF_DEPT,
        )
        user.refresh_from_db()

    response = as_user(tenant_a, user).get(SUMMARY, _window(branch.pk))
    assert response.status_code == 200, response.content
    data = response.json()["data"]

    assert data["students"]["total"] == 2
    assert data["scope"]["departments"] == [
        {"id": department.pk, "name": department.name, "branch": branch.pk}
    ]
    assert "finance" not in data
    assert data["coverage"]["finance"] == {
        "status": "omitted",
        "reason": "insufficient_permission",
        "required_permission": "finance:read",
    }
    assert data["warnings"][-1]["affected_sections"] == ["finance"]


def test_permission_from_remote_membership_cannot_be_borrowed_for_local_scope(
    tenant_a,
    user_in,
    as_user,
):
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        local, _department, _cohort, students = _branch_fixture(
            branch_slug="grant-local",
            department_slug="grant-local-department",
        )
        remote, _remote_department, _remote_cohort, _remote_students = _branch_fixture(
            branch_slug="grant-remote",
            department_slug="grant-remote-department",
        )
        _finance(branch=local, student=students[0])
        user = user_in(tenant_a)
        local_type = _custom_type(
            slug="local-intelligence",
            permissions={"intelligence:read", "students:read", "attendance:read"},
        )
        remote_type = _custom_type(
            slug="remote-finance",
            permissions={"finance:read"},
        )
        RoleMembership.objects.create(
            user=user,
            branch=local,
            role=local_type.compatibility_role,
            account_type=local_type,
        )
        RoleMembership.objects.create(
            user=user,
            branch=remote,
            role=remote_type.compatibility_role,
            account_type=remote_type,
        )
        user.refresh_from_db()
    client = as_user(tenant_a, user)

    local_response = client.get(SUMMARY, _window(local.pk))
    remote_response = client.get(SUMMARY, _window(remote.pk))

    assert local_response.status_code == 200, local_response.content
    assert "finance" not in local_response.json()["data"]
    assert remote_response.status_code == 400
    assert remote_response.json()["errors"] == {"branch": ["Choose an active scope you can access."]}


def test_legacy_grant_and_revoke_overrides_apply_to_each_section_immediately(
    tenant_a,
    user_in,
    as_user,
):
    from apps.access.services import set_override

    with schema_context(tenant_a.schema_name):
        branch, _department, _cohort, students = _branch_fixture(
            branch_slug="legacy-override",
            department_slug="legacy-override-department",
        )
        _finance(branch=branch, student=students[0])
        user = user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=branch)
        # Deliberately retain one pre-account-type assignment to exercise the
        # compatibility override path. Canonical account types own their grants.
        user.role_memberships.update(account_type_id=None)
        set_override(role=Role.HEAD_OF_DEPT, permission="finance:read", effect="grant")
        set_override(role=Role.HEAD_OF_DEPT, permission="attendance:read", effect="revoke")
        user.refresh_from_db()

    response = as_user(tenant_a, user).get(SUMMARY, _window(branch.pk))

    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert "finance" in data
    assert data["coverage"]["finance"]["status"] == "complete"
    assert "attendance" not in data
    assert data["coverage"]["attendance"] == {
        "status": "omitted",
        "reason": "insufficient_permission",
        "required_permission": "attendance:read",
    }


def test_teacher_permission_is_not_a_management_scope(tenant_a, as_role):
    teacher, _user = as_role(Role.TEACHER)

    response = teacher.get(SUMMARY)

    assert response.status_code == 403
    assert response.json()["code"] == "no_authorized_scope"


def test_summary_requires_authentication_and_intelligence_permission(
    tenant_a,
    client_for,
    as_role,
):
    assert client_for(tenant_a).get(SUMMARY).status_code == 401
    cashier, _user = as_role(Role.CASHIER)
    response = cashier.get(SUMMARY)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_strict_filter_validation_and_method_contract(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch, department, _cohort, _students = _branch_fixture(
            branch_slug="validation-local",
            department_slug="validation-local-department",
        )
        other, _other_department, _other_cohort, _other_students = _branch_fixture(
            branch_slug="validation-other",
            department_slug="validation-other-department",
        )
        user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
    client = as_user(tenant_a, user)

    cases = (
        (f"{SUMMARY}?branch=", "branch"),
        (f"{SUMMARY}?branch=0", "branch"),
        (f"{SUMMARY}?branch={branch.pk}&branch={branch.pk}", "branch"),
        (f"{SUMMARY}?date_from=2026-02-30", "date_from"),
        (f"{SUMMARY}?date_from=2026-08-31&date_to=2026-08-01", "date_to"),
        (f"{SUMMARY}?date_from=2025-01-01&date_to=2026-08-01", "date_to"),
        (f"{SUMMARY}?unknown=1", "unknown"),
        (f"{SUMMARY}?branch={other.pk}&department={department.pk}", "department"),
    )
    for url, field in cases:
        response = client.get(url)
        assert response.status_code == 400, (url, response.content)
        assert field in response.json()["errors"]
    assert client.post(SUMMARY, {}, format="json").status_code == 405


def test_private_etag_and_short_cache_revalidation(tenant_a, user_in, as_user):
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        branch, _department, cohort, _students = _branch_fixture(
            branch_slug="etag-local",
            department_slug="etag-local-department",
            student_count=1,
        )
        user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
    client = as_user(tenant_a, user)
    query = _window(branch.pk)

    first = client.get(SUMMARY, query)
    etag = first["ETag"]
    generated_at = first.json()["data"]["generated_at"]
    assert first.json()["data"]["students"]["total"] == 1
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory(branch=branch, current_cohort=cohort)

    cached = client.get(SUMMARY, query)
    conditional = client.get(SUMMARY, query, HTTP_IF_NONE_MATCH=f"W/{etag}")

    assert cached.json()["data"]["students"]["total"] == 1
    assert cached.json()["data"]["generated_at"] == generated_at
    assert cached["ETag"] == etag
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional["ETag"] == etag

    cache.clear()
    refreshed = client.get(SUMMARY, query)
    assert refreshed.json()["data"]["students"]["total"] == 2
    assert refreshed["ETag"] != etag


@pytest.mark.parametrize("failing_operation", ["get", "set"])
def test_cache_outage_does_not_take_down_authoritative_summary(
    tenant_a,
    user_in,
    as_user,
    monkeypatch,
    failing_operation,
):
    with schema_context(tenant_a.schema_name):
        branch, _department, _cohort, _students = _branch_fixture(
            branch_slug=f"cache-outage-{failing_operation}",
            department_slug=f"cache-outage-{failing_operation}-department",
            student_count=1,
        )
        user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)

    def unavailable(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(
        f"apps.intelligence.views.v1.intelligence_views.cache.{failing_operation}",
        unavailable,
    )
    response = as_user(tenant_a, user).get(SUMMARY, _window(branch.pk))

    assert response.status_code == 200, response.content
    assert response.json()["data"]["students"]["total"] == 1
    assert response["ETag"].startswith('"')


def test_cache_isolated_by_tenant_scope_permissions_and_locale(
    tenant_a,
    tenant_b,
    user_in,
    as_user,
):
    from apps.users.models import RoleMembership

    with schema_context(tenant_a.schema_name):
        branch_a, _department_a, _cohort_a, _students_a = _branch_fixture(
            branch_slug="cache-a",
            department_slug="cache-a-department",
            student_count=1,
        )
        branch_a_other, _department_a_other, _cohort_a_other, _students_a_other = _branch_fixture(
            branch_slug="cache-a-other-scope",
            department_slug="cache-a-other-department",
            student_count=2,
        )
        director_a = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch_a)
        limited = user_in(tenant_a)
        limited_type = _custom_type(
            slug="cache-limited",
            permissions={"intelligence:read", "students:read"},
        )
        RoleMembership.objects.create(
            user=limited,
            branch=branch_a,
            role=limited_type.compatibility_role,
            account_type=limited_type,
        )
        limited.refresh_from_db()
    with schema_context(tenant_b.schema_name):
        branch_b, _department_b, _cohort_b, _students_b = _branch_fixture(
            branch_slug="cache-b",
            department_slug="cache-b-department",
            student_count=3,
        )
        director_b = user_in(tenant_b, roles=[Role.DIRECTOR], branch=branch_b)

    director_a_client = as_user(tenant_a, director_a)
    limited_client = as_user(tenant_a, limited)
    director_b_client = as_user(tenant_b, director_b)
    a_en = director_a_client.get(
        SUMMARY,
        _window(branch_a.pk),
        HTTP_ACCEPT_LANGUAGE="en",
    )
    a_ru = director_a_client.get(
        SUMMARY,
        _window(branch_a.pk),
        HTTP_ACCEPT_LANGUAGE="ru",
    )
    a_other_scope = director_a_client.get(SUMMARY, _window(branch_a_other.pk))
    limited_response = limited_client.get(SUMMARY, _window(branch_a.pk))
    b_response = director_b_client.get(SUMMARY, _window(branch_b.pk))

    assert a_en.json()["data"]["locale"] == "en"
    assert a_ru.json()["data"]["locale"] == "ru"
    assert a_en["ETag"] != a_ru["ETag"]
    assert a_other_scope.json()["data"]["students"]["total"] == 2
    assert a_en["ETag"] != a_other_scope["ETag"]
    assert "finance" in a_en.json()["data"]
    assert "finance" not in limited_response.json()["data"]
    assert limited_response.json()["data"]["coverage"]["capacity"]["reason"] == ("insufficient_permission")
    assert limited_response.json()["data"]["coverage"]["risk"]["reason"] == ("insufficient_permission")
    assert limited_response.json()["data"]["coverage"]["teachers"]["reason"] == ("insufficient_permission")
    assert a_en["ETag"] != limited_response["ETag"]
    assert a_en.json()["data"]["students"]["total"] == 1
    assert b_response.json()["data"]["students"]["total"] == 3
    assert a_en["ETag"] != b_response["ETag"]


def test_personal_attention_is_exact_principal_scoped_and_cache_isolated(
    tenant_a,
    user_in,
    as_user,
):
    from apps.approvals.models import ApprovalRequest
    from apps.meetings.models import MeetingAttendee, StaffMeeting
    from apps.notifications.models import Notification, RecipientAttributionStatus
    from apps.tasks.models import Task

    with schema_context(tenant_a.schema_name):
        branch, _department, _cohort, _students = _branch_fixture(
            branch_slug="principal-cache",
            department_slug="principal-cache-department",
            student_count=1,
        )
        first = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
        second = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
        first.refresh_from_db()
        second.refresh_from_db()
        first_principal = first.staff_profile
        Notification.objects.create(
            user=first,
            event_type="approval.approved",
            title="Leadership update",
            recipient_principal_kind="staff",
            recipient_principal_id=first_principal.pk,
            attribution_status=RecipientAttributionStatus.CAPTURED,
        )
        Task.objects.create(
            title="Executive action",
            branch=branch,
            assignee=first,
            assignee_principal_kind="staff",
            assignee_principal_id=first_principal.pk,
            assignee_attribution_status="captured",
            created_by=first,
            created_by_principal_kind="staff",
            created_by_principal_id=first_principal.pk,
            created_by_attribution_status=Task.CreatorAttributionStatus.CAPTURED,
        )
        ApprovalRequest.objects.create(
            kind="other",
            branch=branch,
            requested_by=first,
            title="Approve policy exception",
        )
        starts_at = timezone.now() + timedelta(days=2)
        meeting = StaffMeeting.objects.create(
            title="Leadership review",
            branch=branch,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            created_by=first,
            created_by_principal_kind="staff",
            created_by_principal_id=first_principal.pk,
            created_by_attribution_status=StaffMeeting.ActorAttributionStatus.CAPTURED,
        )
        MeetingAttendee.objects.create(
            meeting=meeting,
            user=first,
            principal_kind="staff",
            principal_id=first_principal.pk,
        )

    cache.clear()
    today = timezone.localdate()
    query = {
        "date_from": today.isoformat(),
        "date_to": (today + timedelta(days=30)).isoformat(),
    }
    first_response = as_user(tenant_a, first).get(SUMMARY, query)
    second_response = as_user(tenant_a, second).get(SUMMARY, query)

    assert first_response.status_code == 200, first_response.content
    assert second_response.status_code == 200, second_response.content
    first_attention = first_response.json()["data"]["attention"]
    second_attention = second_response.json()["data"]["attention"]
    assert first_attention["unread_notifications"] == 1
    assert first_attention["tasks"]["open_assigned_to_me"] == 1
    assert first_attention["upcoming_meetings"] == 1
    assert first_attention["pending_approvals"] == 1
    assert second_attention["unread_notifications"] == 0
    assert second_attention["tasks"]["open_assigned_to_me"] == 0
    assert second_attention["upcoming_meetings"] == 0
    assert second_attention["pending_approvals"] == 1
    assert first_response["ETag"] != second_response["ETag"]


def test_query_count_is_population_invariant(tenant_a, user_in, client_for):
    from apps.students.tests.factories import StudentProfileFactory
    from tests.role_principal_helpers import ensure_role_principal, exact_session_client

    with schema_context(tenant_a.schema_name):
        branch, _department, cohort, _students = _branch_fixture(
            branch_slug="query-count",
            department_slug="query-count-department",
            student_count=1,
        )
        user = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch)
        ensure_role_principal(user, roles=[Role.DIRECTOR], branch=branch)
    client = exact_session_client(client_for, tenant_a, user)
    query = _window(branch.pk)

    cache.clear()
    with CaptureQueriesContext(connection) as small_capture:
        small = client.get(SUMMARY, query)
    with schema_context(tenant_a.schema_name):
        StudentProfileFactory.create_batch(40, branch=branch, current_cohort=cohort)
    cache.clear()
    with CaptureQueriesContext(connection) as large_capture:
        large = client.get(SUMMARY, query)

    assert small.status_code == 200, small.content
    assert large.status_code == 200, large.content
    assert large.json()["data"]["students"]["total"] == 41
    assert len(large_capture) <= len(small_capture) + 1
    assert len(large_capture) <= 40
