"""F13-1 — dynamic per-teacher payout/salary engine: a configurable pay rule (hourly /
% of collected tuition / flat), computed per period, routed through the A-1 approvals
engine (a manager approves, a cashier disburses; the teacher never pays themselves)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

POLICY = "/api/v1/teachers/{}/payout-policy/"
PREPARE = "/api/v1/teachers/{}/prepare-salary/"


def _teacher(tenant, branch=None):
    from apps.org.tests.factories import BranchFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant.schema_name):
        branch = branch or BranchFactory()
        return TeacherProfileFactory(branch=branch), branch


def _wide_period():
    today = timezone.localdate()
    return today - timedelta(days=1), today + timedelta(days=1)


# --- policy CRUD + validation --------------------------------------------
def test_set_and_get_hourly_policy(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher, _b = _teacher(tenant_a)
    r = director.put(
        POLICY.format(teacher.id), {"method": "hourly", "hourly_rate_uzs": "50000"}, format="json"
    )
    assert r.status_code == 200, r.content
    assert r.json()["data"]["method"] == "hourly"
    assert r.json()["data"]["hourly_rate_uzs"] == "50000.00"
    assert director.get(POLICY.format(teacher.id)).json()["data"]["hourly_rate_uzs"] == "50000.00"


def test_method_requires_its_params(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher, _b = _teacher(tenant_a)
    assert director.put(POLICY.format(teacher.id), {"method": "hourly"}, format="json").status_code == 400
    assert (
        director.put(
            POLICY.format(teacher.id),
            {"method": "percent_of_collected_tuition", "tuition_percent": "150"},
            format="json",
        ).status_code
        == 400
    )
    assert director.put(POLICY.format(teacher.id), {"method": "bogus"}, format="json").status_code == 400


# --- compute (all three methods) -----------------------------------------
def test_compute_hourly(tenant_a):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.services import compute_payout, set_payout_policy

    teacher, branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        term = TermFactory()
        base = timezone.now()
        for i in range(2):  # two 1-hour lessons = 2 taught hours
            start = base + timedelta(hours=i)
            Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=teacher,
                title="L",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
            )
        set_payout_policy(teacher=teacher, method="hourly", hourly_rate_uzs=Decimal("50000"))
        start_d, end_d = _wide_period()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["method"] == "hourly"
        assert result["amount_uzs"] == Decimal("100000.00")  # 2h x 50000


def test_compute_flat(tenant_a):
    from apps.teachers.services import compute_payout, set_payout_policy

    teacher, _b = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(teacher=teacher, method="flat_monthly", flat_amount_uzs=Decimal("3000000"))
        start_d, end_d = _wide_period()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["amount_uzs"] == Decimal("3000000.00")


def test_compute_percent_of_collected_tuition(tenant_a):
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.finance.models import PaymentAllocation
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.services import compute_payout, set_payout_policy

    teacher, branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)  # active member of the teacher's cohort
        invoice = InvoiceFactory(student=student, cohort=cohort)
        # 400,000 collected (allocated) toward this cohort's tuition, created now.
        PaymentAllocation.objects.create(invoice=invoice, payment_id=1, amount_uzs=Decimal("400000.00"))
        set_payout_policy(
            teacher=teacher, method="percent_of_collected_tuition", tuition_percent=Decimal("40")
        )
        start_d, end_d = _wide_period()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["amount_uzs"] == Decimal("160000.00")  # 40% of 400,000


def test_percent_only_counts_the_teachers_own_cohort_tuition(tenant_a):
    """Regression (self-review): tuition a student paid for ANOTHER teacher's course must
    NOT count toward this teacher — the sum is scoped per cohort (Invoice.cohort), so the
    total payout can't exceed the tuition actually collected."""
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.finance.models import PaymentAllocation
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.services import compute_payout, set_payout_policy
    from apps.teachers.tests.factories import TeacherProfileFactory

    teacher, branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        my_cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        other_cohort = CohortFactory(branch=branch, primary_teacher=TeacherProfileFactory(branch=branch))
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=my_cohort, student=student)
        CohortMembershipFactory(cohort=other_cohort, student=student)  # also in the OTHER course
        # 100k paid for MY cohort, 900k paid for the OTHER teacher's cohort.
        PaymentAllocation.objects.create(
            invoice=InvoiceFactory(student=student, cohort=my_cohort),
            payment_id=1,
            amount_uzs=Decimal("100000.00"),
        )
        PaymentAllocation.objects.create(
            invoice=InvoiceFactory(student=student, cohort=other_cohort),
            payment_id=2,
            amount_uzs=Decimal("900000.00"),
        )
        set_payout_policy(
            teacher=teacher, method="percent_of_collected_tuition", tuition_percent=Decimal("50")
        )
        start_d, end_d = _wide_period()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["amount_uzs"] == Decimal("50000.00")  # 50% of only MY cohort's 100k


def test_prepare_salary_rejects_a_max_year_period(tenant_a, as_role):
    """Regression (self-review, never-500): period_end at date.max would overflow
    period_end+1day; must be a clean 400, not a 500, on the money endpoint."""
    director, _ = as_role(Role.DIRECTOR)
    teacher, _b = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy_flat(teacher)
    r = director.post(
        PREPARE.format(teacher.id),
        {"period_start": "2020-01-01", "period_end": "9999-12-31"},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "validation_error"


def set_payout_policy_flat(teacher):
    from apps.teachers.services import set_payout_policy

    set_payout_policy(teacher=teacher, method="flat_monthly", flat_amount_uzs=Decimal("1000000"))


def test_generic_approvals_endpoint_cannot_mint_a_salary(tenant_a, as_role):
    """Regression (self-review): salary_prep must NOT be creatable via the generic
    POST /approvals/ (only the computed + branch-scoped /prepare-salary/ path) — otherwise
    an approvals:write user could mint a raw, uncomputed, unscoped money-OUT salary."""
    director, _ = as_role(Role.DIRECTOR)  # holds approvals:write (*:*)
    r = director.post(
        "/api/v1/approvals/requests/",
        {
            "kind": "salary_prep",
            "title": "x",
            "amount_uzs": "50000000.00",
            "payload": {"teacher_profile_id": 999, "party_label": "Ghost"},
        },
        format="json",
    )
    assert r.status_code == 400  # not an allowed generic kind


# --- prepare -> A-1 + SoD -------------------------------------------------
def test_prepare_salary_creates_and_flows_through_approvals(tenant_a, as_role):
    from apps.approvals.models import ApprovalRequest
    from apps.teachers.services import set_payout_policy

    director, _ = as_role(Role.DIRECTOR)
    teacher, _b = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(teacher=teacher, method="flat_monthly", flat_amount_uzs=Decimal("2500000"))
    start_d, end_d = _wide_period()

    r = director.post(
        PREPARE.format(teacher.id),
        {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()},
        format="json",
    )
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["kind"] == "salary_prep"
    assert body["amount_uzs"] == "2500000.00"
    rid = body["request_id"]
    with schema_context(tenant_a.schema_name):
        req = ApprovalRequest.objects.get(pk=rid)
        assert req.kind == "salary_prep"
        assert req.amount_uzs == Decimal("2500000.00")
        assert req.payload["teacher_profile_id"] == teacher.id  # SoD beneficiary pinned


def test_teacher_cannot_approve_their_own_salary(tenant_a, user_in, as_user):
    """SoD extends to the beneficiary: the teacher (even with approve rights) can't sign off
    their own salary payout."""
    from apps.approvals.services import approve
    from apps.teachers.models import TeacherProfile
    from apps.teachers.services import prepare_salary, set_payout_policy
    from core.exceptions import PermissionException

    # A director-role user who is ALSO the teacher (holds approve rights + is the beneficiary).
    teacher_user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        from apps.org.tests.factories import BranchFactory

        teacher = TeacherProfile.objects.create(user=teacher_user, branch=BranchFactory())
        set_payout_policy(teacher=teacher, method="flat_monthly", flat_amount_uzs=Decimal("1000000"))
        start_d, end_d = _wide_period()
        req = prepare_salary(teacher=teacher, period_start=start_d, period_end=end_d, requested_by=None)
        with pytest.raises(PermissionException) as exc:
            approve(request_id=req.pk, actor=teacher_user)
        assert exc.value.code == "salary_self_dealing"


def test_zero_payout_is_rejected(tenant_a):
    """An hourly teacher with no taught hours in the period computes to 0 -> nothing to prepare."""
    from apps.teachers.services import prepare_salary, set_payout_policy
    from core.exceptions import UnprocessableEntity

    teacher, _b = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(teacher=teacher, method="hourly", hourly_rate_uzs=Decimal("50000"))
        start_d, end_d = _wide_period()
        with pytest.raises(UnprocessableEntity) as exc:
            prepare_salary(teacher=teacher, period_start=start_d, period_end=end_d)
        assert exc.value.code == "zero_payout"
