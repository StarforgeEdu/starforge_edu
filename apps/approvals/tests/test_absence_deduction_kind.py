"""A-1 money feature — the `absence_deduction` KIND of the Approvals engine (F23-1).

Dignity DNA: a student should not pay for teaching they did not receive. When a center
opts into the policy, a manager may request a credit for a lesson the student missed;
approving it materializes a standing finance.Discount (a negative invoice line) the same
way a discount does. Anti-fraud DNA: the deduction must cite a REAL absence for that
student, the center must allow it (and may require the absence be excused), and a given
absence can be credited only once.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.attendance.models import AttendanceRecord
from core.permissions import Role

pytestmark = pytest.mark.django_db

REQ = "/api/v1/approvals/requests/"


def _student_id(tenant) -> int:
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        return StudentProfileFactory.create().id


def _set_policy(tenant, *, enabled=True, excused_only=True) -> None:
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        s = CenterSettings.load()
        s.absence_deduction_enabled = enabled
        s.absence_deduction_excused_only = excused_only
        s.save()  # the org receiver clears the cached settings on write


def _absence(tenant, *, student_id, status=AttendanceRecord.Status.EXCUSED) -> int:
    """A real absence record for `student_id` (its own lesson), returning its id."""
    from apps.attendance.tests.factories import AttendanceRecordFactory
    from apps.cohorts.tests.factories import CohortFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.models import StudentProfile
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant.schema_name):
        student = StudentProfile.objects.get(pk=student_id)
        teacher = TeacherProfileFactory.create(branch=student.branch)
        cohort = CohortFactory.create(branch=student.branch)
        term = TermFactory.create()
        start = timezone.now() + timedelta(days=1)
        lesson = Lesson.objects.create(
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="Algebra",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        return AttendanceRecordFactory.create(student=student, lesson=lesson, status=status).id


def _request(client, *, student_id, attendance_id, amount="50000"):
    return client.post(
        REQ,
        {
            "kind": "absence_deduction",
            "title": "Missed lesson credit",
            "payload": {
                "student_id": student_id,
                "attendance_id": attendance_id,
                "fixed_amount_uzs": amount,
            },
        },
        format="json",
    )


def test_request_rejected_when_center_has_not_opted_in(tenant_a, as_role):
    """The policy is OFF by default — a center must explicitly enable absence deductions."""
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)
    r = _request(teacher, student_id=sid, attendance_id=aid)
    assert r.status_code == 400, r.content
    assert r.json()["code"] == "absence_deduction_disabled"


def test_excused_only_policy_rejects_a_plain_absence(tenant_a, as_role):
    """With the default excused-only policy, an unexcused (plain ABSENT) record does not
    qualify — only an absence with an accepted reason (EXCUSED) earns a credit."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid, status=AttendanceRecord.Status.ABSENT)
    r = _request(teacher, student_id=sid, attendance_id=aid)
    assert r.status_code == 400
    assert r.json()["code"] == "absence_deduction_requires_excuse"


def test_plain_absence_allowed_when_policy_does_not_require_excuse(tenant_a, as_role):
    _set_policy(tenant_a, enabled=True, excused_only=False)
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid, status=AttendanceRecord.Status.ABSENT)
    r = _request(teacher, student_id=sid, attendance_id=aid)
    assert r.status_code == 201, r.content
    assert r.json()["data"]["amount_uzs"] is None  # decision-only — never disburses


def test_a_present_record_is_not_an_absence(tenant_a, as_role):
    _set_policy(tenant_a, enabled=True, excused_only=False)
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid, status=AttendanceRecord.Status.PRESENT)
    r = _request(teacher, student_id=sid, attendance_id=aid)
    assert r.status_code == 400
    assert r.json()["code"] == "absence_deduction_attendance_invalid"


def test_attendance_must_belong_to_the_named_student(tenant_a, as_role):
    """Anti-fraud: you cannot pin another student's absence to this student's deduction."""
    _set_policy(tenant_a, enabled=True, excused_only=False)
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    other_sid = _student_id(tenant_a)
    aid_other = _absence(tenant_a, student_id=other_sid)
    r = _request(teacher, student_id=sid, attendance_id=aid_other)
    assert r.status_code == 400
    assert r.json()["code"] == "absence_deduction_attendance_invalid"


def test_approving_materializes_a_credit_discount(tenant_a, as_role):
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    director, director_user = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)

    rid = _request(teacher, student_id=sid, attendance_id=aid, amount="45000").json()["data"]["id"]
    ap = director.post(f"{REQ}{rid}/approve/", {"note": "missed a paid lesson"}, format="json")
    assert ap.status_code == 200, ap.content
    discount_id = ap.json()["data"]["payload"]["discount_id"]
    assert discount_id

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        d = Discount.objects.get(pk=discount_id)
        assert d.student_id == sid
        assert d.fixed_amount_uzs == Decimal("45000")
        assert d.percent is None
        assert d.discount_type == "manual"
        assert d.approved_by_id == director_user.id
        assert d.is_active is True
        assert d.single_use is True  # a one-time credit, not a standing scholarship


def test_correcting_the_absence_retires_the_unclaimed_credit(tenant_a, as_role):
    """R2-05: if the absence is later CORRECTED (student was actually present), the
    single-use credit must be retired so the student isn't credited for a lesson they
    attended. clear_absence_deduction (called by mark_attendance on an absent->present
    transition) retires a still-active credit and stamps the audit trail."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)
    rid = _request(teacher, student_id=sid, attendance_id=aid, amount="45000").json()["data"]["id"]
    discount_id = director.post(f"{REQ}{rid}/approve/", {}, format="json").json()["data"]["payload"][
        "discount_id"
    ]

    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.approvals.services import clear_absence_deduction
        from apps.finance.models import Discount

        assert Discount.objects.get(pk=discount_id).is_active is True
        retired = clear_absence_deduction(attendance_id=aid)
        assert retired is True
        assert Discount.objects.get(pk=discount_id).is_active is False  # credit retired
        # audit trail links the retirement back to the correction
        assert ApprovalRequest.objects.get(pk=rid).payload.get("credit_cleared") == "attendance_corrected"
        # idempotent: a second correction is a no-op (credit already retired)
        assert clear_absence_deduction(attendance_id=aid) is False


def test_approve_re_enforces_excused_only_after_excuse_revoked(tenant_a, as_role):
    """R6-02: an excused-only center creates a deduction for an EXCUSED record; if the
    excuse is revoked (EXCUSED->ABSENT) before approval, approving must be rejected — the
    approve-time re-check enforces the excused-only policy, not just absence-ness."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid, status=AttendanceRecord.Status.EXCUSED)
    rid = _request(teacher, student_id=sid, attendance_id=aid).json()["data"]["id"]

    # Excuse revoked: EXCUSED -> ABSENT (an unexcused absence under an excused-only policy).
    with schema_context(tenant_a.schema_name):
        AttendanceRecord.objects.filter(pk=aid).update(status=AttendanceRecord.Status.ABSENT)

    resp = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert resp.status_code == 422, resp.content
    assert resp.json()["code"] == "absence_deduction_requires_excuse"


def test_credit_applies_to_one_invoice_then_retires(tenant_a, as_role):
    """The defining property of an absence deduction: it credits the ONE missed lesson
    exactly once. A single-use Discount reduces the next invoice and then retires, so it
    never recurs on every future bill the way a standing scholarship would."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)
    rid = _request(teacher, student_id=sid, attendance_id=aid, amount="45000").json()["data"]["id"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount, InvoiceLine
        from apps.finance.services import issue_invoice

        line = {
            "description": "Tuition",
            "line_type": InvoiceLine.LineType.TUITION,
            "quantity": "1",
            "unit_price_uzs": "500000",
        }
        first = issue_invoice(student_id=sid, lines=[line])
        second = issue_invoice(student_id=sid, lines=[line])
        assert first.total_uzs == Decimal("455000.00")  # 500000 - 45000 credit
        assert second.total_uzs == Decimal("500000.00")  # NOT credited again
        assert Discount.objects.get(student_id=sid).is_active is False  # retired after one use


def test_non_dict_payload_is_a_clean_400_not_a_500(tenant_a, as_role):
    """A non-object payload (a JSON string/array — valid JSON the serializer accepts) must
    not reach a kind validator's .get() and 500; it is a clean 400 for every kind."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    r = teacher.post(
        REQ,
        {"kind": "absence_deduction", "title": "x", "payload": "oops-not-an-object"},
        format="json",
    )
    assert r.status_code == 400, r.content
    assert r.json()["code"] == "payload_invalid"


def test_an_absence_cannot_be_deducted_twice(tenant_a, as_role):
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)
    assert _request(teacher, student_id=sid, attendance_id=aid).status_code == 201
    second = _request(teacher, student_id=sid, attendance_id=aid)
    assert second.status_code == 400
    assert second.json()["code"] == "absence_deduction_duplicate"


def test_rejecting_deactivates_the_credit_and_frees_the_absence(tenant_a, as_role):
    """A reversed deduction stops crediting (the Discount deactivates) and the absence
    becomes deductible again — a fresh request is no longer a duplicate."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)
    rid = _request(teacher, student_id=sid, attendance_id=aid).json()["data"]["id"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(student_id=sid).is_active is True

    rej = director.post(f"{REQ}{rid}/reject/", {"note": "wrong lesson"}, format="json")
    assert rej.status_code == 200
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(student_id=sid).is_active is False
    # the absence can now be re-requested (the rejected one no longer blocks it)
    assert _request(teacher, student_id=sid, attendance_id=aid).status_code == 201


def test_non_finite_amount_is_a_clean_400_not_a_500(tenant_a, as_role):
    """A non-finite Decimal in the freeform payload is unordered — a range comparison
    would raise InvalidOperation (a 500). It must be a clean 400."""
    _set_policy(tenant_a, enabled=True, excused_only=True)
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    aid = _absence(tenant_a, student_id=sid)
    r = _request(teacher, student_id=sid, attendance_id=aid, amount="NaN")
    assert r.status_code == 400, r.content
    assert r.json()["code"] == "absence_deduction_amount_invalid"
