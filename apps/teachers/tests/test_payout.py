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


def _completed_month():
    first_this_month = timezone.localdate().replace(day=1)
    last_completed_month = first_this_month - timedelta(days=1)
    return last_completed_month.replace(day=1), last_completed_month


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


def test_directory_reader_cannot_read_payout_policy(tenant_a, user_in, as_user):
    from apps.teachers.services import set_payout_policy

    teacher, branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(teacher=teacher, method="flat_monthly", flat_amount_uzs=Decimal("3000000"))
    registrar = as_user(tenant_a, user_in(tenant_a, roles=[Role.REGISTRAR], branch=branch))

    assert registrar.get(POLICY.format(teacher.id)).status_code == 403


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
                status=Lesson.Status.COMPLETED,
            )
        set_payout_policy(teacher=teacher, method="hourly", hourly_rate_uzs=Decimal("50000"))
        start_d, end_d = _wide_period()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["method"] == "hourly"
        assert result["amount_uzs"] == Decimal("100000.00")  # 2h x 50000


def test_hourly_payout_counts_only_completed_work_and_rounds_money_once(tenant_a):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.services import compute_payout, set_payout_policy

    teacher, branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        term = TermFactory()
        base = timezone.now() - timedelta(hours=2)
        # One delivered minute at 60,000/hour is exactly 1,000. Pre-rounding
        # hours to 0.02 would incorrectly pay 1,200.
        Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="Delivered",
            starts_at=base,
            ends_at=base + timedelta(minutes=1),
            status=Lesson.Status.COMPLETED,
        )
        # A scheduled hour is not evidence of delivered work and must not be
        # turned into a salary liability.
        Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="Scheduled only",
            starts_at=base + timedelta(hours=1),
            ends_at=base + timedelta(hours=2),
            status=Lesson.Status.SCHEDULED,
        )
        set_payout_policy(teacher=teacher, method="hourly", hourly_rate_uzs=Decimal("60000"))
        start_d, end_d = _wide_period()

        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)

    assert result["amount_uzs"] == Decimal("1000.00")
    assert result["breakdown"]["hours"] == "0.0167"


def test_compute_flat(tenant_a):
    from apps.teachers.services import compute_payout, set_payout_policy

    teacher, _b = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(teacher=teacher, method="flat_monthly", flat_amount_uzs=Decimal("3000000"))
        start_d, end_d = _completed_month()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["amount_uzs"] == Decimal("3000000.00")


def test_flat_monthly_rejects_partial_or_open_months(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher, _branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy_flat(teacher)
    today = timezone.localdate()

    partial = director.post(
        PREPARE.format(teacher.pk),
        {
            "period_start": (today - timedelta(days=2)).isoformat(),
            "period_end": (today - timedelta(days=1)).isoformat(),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-partial-month-0001",
    )
    first_this_month = today.replace(day=1)
    open_month = director.post(
        PREPARE.format(teacher.pk),
        {
            "period_start": first_this_month.isoformat(),
            "period_end": (
                (first_this_month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            ).isoformat(),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-open-month-0001",
    )

    assert partial.status_code == 400
    assert set(partial.json()["errors"]) == {"period_start", "period_end"}
    assert open_month.status_code == 400
    assert set(open_month.json()["errors"]) == {"period_end"}


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


@pytest.mark.parametrize(
    "method",
    ["hourly", "percent_of_collected_tuition", "flat_monthly", "corrupt-method"],
)
def test_compute_payout_fails_closed_on_corrupt_active_policy(tenant_a, method):
    from apps.teachers.models import PayoutPolicy
    from apps.teachers.services import compute_payout
    from core.exceptions import UnprocessableEntity

    teacher, _branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        PayoutPolicy.objects.create(teacher=teacher, method=method)
        start_d, end_d = _completed_month()
        with pytest.raises(UnprocessableEntity) as exc:
            compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)

    assert exc.value.code == "invalid_payout_policy"


def test_percent_payout_uses_custom_typed_cohort_assignment(tenant_a):
    from apps.cohorts.models import CohortTeacher
    from apps.cohorts.tests.factories import CohortFactory
    from apps.finance.models import PaymentAllocation
    from apps.finance.tests.factories import InvoiceFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.services import compute_payout, set_payout_policy
    from apps.teachers.tests.factories import TeacherTypeFactory

    teacher, branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(branch=branch)
        CohortTeacher.objects.create(
            cohort=cohort,
            teacher=teacher,
            teacher_type=TeacherTypeFactory(name="Workshop Lead", slug="workshop-lead"),
        )
        student = StudentProfileFactory(branch=branch)
        invoice = InvoiceFactory(student=student, cohort=cohort)
        PaymentAllocation.objects.create(
            invoice=invoice,
            payment_id=1,
            amount_uzs=Decimal("200000.00"),
        )
        set_payout_policy(
            teacher=teacher,
            method="percent_of_collected_tuition",
            tuition_percent=Decimal("25"),
        )
        start_d, end_d = _wide_period()
        result = compute_payout(teacher=teacher, period_start=start_d, period_end=end_d)
        assert result["amount_uzs"] == Decimal("50000.00")


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
        HTTP_IDEMPOTENCY_KEY="salary-max-date-0001",
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
    start_d, end_d = _completed_month()

    r = director.post(
        PREPARE.format(teacher.id),
        {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-prepare-flow-0001",
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


def test_prepare_salary_is_idempotent_by_key_and_teacher_period(tenant_a, as_role):
    from apps.approvals.models import ApprovalRequest
    from apps.teachers.services import set_payout_policy

    director, _ = as_role(Role.DIRECTOR)
    teacher, _branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(
            teacher=teacher,
            method="flat_monthly",
            flat_amount_uzs=Decimal("2500000"),
        )
    start_d, end_d = _completed_month()
    payload = {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()}

    first = director.post(
        PREPARE.format(teacher.id),
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-idempotency-0001",
    )
    same_key = director.post(
        PREPARE.format(teacher.id),
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-idempotency-0001",
    )
    new_key_same_period = director.post(
        PREPARE.format(teacher.id),
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-idempotency-0002",
    )

    assert first.status_code == same_key.status_code == new_key_same_period.status_code == 201
    request_ids = {
        response.json()["data"]["request_id"] for response in (first, same_key, new_key_same_period)
    }
    assert len(request_ids) == 1
    with schema_context(tenant_a.schema_name):
        request = ApprovalRequest.objects.get(pk=request_ids.pop())
        assert ApprovalRequest.objects.filter(kind="salary_prep").count() == 1
        assert request.idempotency_key_hash != "salary-idempotency-0001"
        assert len(request.idempotency_key_hash or "") == 64


def test_prepare_salary_rejects_overlapping_periods(tenant_a, as_role):
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.teachers.services import set_payout_policy

    director, _ = as_role(Role.DIRECTOR)
    teacher, branch = _teacher(tenant_a)
    today = timezone.localdate()
    with schema_context(tenant_a.schema_name):
        cohort = CohortFactory(branch=branch, primary_teacher=teacher)
        start = timezone.now() - timedelta(hours=1)
        Lesson.objects.create(
            term=TermFactory(),
            cohort=cohort,
            teacher=teacher,
            title="Delivered",
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status=Lesson.Status.COMPLETED,
        )
        set_payout_policy(teacher=teacher, method="hourly", hourly_rate_uzs=Decimal("50000"))

    first = director.post(
        PREPARE.format(teacher.pk),
        {
            "period_start": (today - timedelta(days=2)).isoformat(),
            "period_end": today.isoformat(),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-overlap-first-0001",
    )
    overlap = director.post(
        PREPARE.format(teacher.pk),
        {
            "period_start": today.isoformat(),
            "period_end": (today + timedelta(days=1)).isoformat(),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-overlap-next-0001",
    )

    assert first.status_code == 201, first.content
    assert overlap.status_code == 409
    assert overlap.json()["code"] == "salary_period_overlap"


def test_prepare_salary_rejects_missing_or_reused_mismatched_idempotency_key(
    tenant_a,
    as_role,
):
    from apps.teachers.services import set_payout_policy

    director, _ = as_role(Role.DIRECTOR)
    teacher, _branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        set_payout_policy(
            teacher=teacher,
            method="flat_monthly",
            flat_amount_uzs=Decimal("2500000"),
        )
    start_d, end_d = _completed_month()

    missing = director.post(
        PREPARE.format(teacher.id),
        {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()},
        format="json",
    )
    first = director.post(
        PREPARE.format(teacher.id),
        {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()},
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-mismatch-0001",
    )
    mismatched = director.post(
        PREPARE.format(teacher.id),
        {
            "period_start": (start_d - timedelta(days=10)).isoformat(),
            "period_end": (end_d - timedelta(days=10)).isoformat(),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-mismatch-0001",
    )
    padded = director.post(
        PREPARE.format(teacher.id),
        {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()},
        format="json",
        HTTP_IDEMPOTENCY_KEY=" salary-mismatch-0002 ",
    )

    assert missing.status_code == 400
    assert set(missing.json()["errors"]) == {"Idempotency-Key"}
    assert padded.status_code == 400
    assert set(padded.json()["errors"]) == {"Idempotency-Key"}
    assert first.status_code == 201
    assert mismatched.status_code == 409
    assert mismatched.json()["code"] == "idempotency_mismatch"


def test_prepare_salary_checks_key_owner_before_lower_id_domain_match(
    tenant_a,
    as_role,
):
    """A lower-id domain match must not hide that another request owns the key."""
    from apps.approvals.models import ApprovalRequest
    from apps.teachers.services import set_payout_policy

    director, _ = as_role(Role.DIRECTOR)
    first_teacher, _first_branch = _teacher(tenant_a)
    key_owner_teacher, _owner_branch = _teacher(tenant_a)
    with schema_context(tenant_a.schema_name):
        for teacher in (first_teacher, key_owner_teacher):
            set_payout_policy(
                teacher=teacher,
                method="flat_monthly",
                flat_amount_uzs=Decimal("2500000"),
            )
    start_d, end_d = _completed_month()
    payload = {"period_start": start_d.isoformat(), "period_end": end_d.isoformat()}

    lower_id_domain = director.post(
        PREPARE.format(first_teacher.pk),
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-domain-first-0001",
    )
    key_owner = director.post(
        PREPARE.format(key_owner_teacher.pk),
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-shared-owner-0001",
    )
    assert lower_id_domain.status_code == key_owner.status_code == 201
    assert lower_id_domain.json()["data"]["request_id"] < key_owner.json()["data"]["request_id"]

    reused_for_lower_id_domain = director.post(
        PREPARE.format(first_teacher.pk),
        payload,
        format="json",
        HTTP_IDEMPOTENCY_KEY="salary-shared-owner-0001",
    )

    assert reused_for_lower_id_domain.status_code == 409
    assert reused_for_lower_id_domain.json()["code"] == "idempotency_mismatch"
    with schema_context(tenant_a.schema_name):
        assert ApprovalRequest.objects.filter(kind="salary_prep").count() == 2


def test_payout_policy_rejects_json_numbers_and_silent_rounding(tenant_a, as_role):
    director, _ = as_role(Role.DIRECTOR)
    teacher, _branch = _teacher(tenant_a)

    json_number = director.put(
        POLICY.format(teacher.id),
        {"method": "hourly", "hourly_rate_uzs": 50000},
        format="json",
    )
    subunit = director.put(
        POLICY.format(teacher.id),
        {"method": "hourly", "hourly_rate_uzs": "50000.001"},
        format="json",
    )

    assert json_number.status_code == 400
    assert set(json_number.json()["errors"]) == {"hourly_rate_uzs"}
    assert subunit.status_code == 400
    assert set(subunit.json()["errors"]) == {"hourly_rate_uzs"}


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
        start_d, end_d = _completed_month()
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
