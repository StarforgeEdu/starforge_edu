"""A-1 money feature — the `fine` KIND of the Approvals engine.

A fine (a monetary penalty against a student) needs sign-off (anti-fraud DNA: no
silently-imposed charges). It is a decision-only request: approving it issues a
one-off PENALTY invoice the student owes, collected through the normal payments
machinery. The approval row is the permanent paper trail (who fined, who approved,
why). A scholarship must never shrink a punishment, so the fine invoice carries no
discount lines; overturning an approved fine voids the invoice — unless the student
already paid it, in which case the reversal is refused (use the refund flow)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.approvals import services
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

REQ = "/api/v1/approvals/requests/"


def _student_id(tenant) -> int:
    with schema_context(tenant.schema_name):
        return StudentProfileFactory.create().id


def _raise_fine(client, sid, *, amount="50000", reason="Repeated lateness"):
    return client.post(
        REQ,
        {
            "kind": "fine",
            "title": "Lateness fine",
            "amount_uzs": amount,
            "payload": {"student_id": sid, "reason": reason},
        },
        format="json",
    )


def test_approving_fine_issues_a_penalty_invoice(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)

    r = _raise_fine(teacher, sid, amount="75000")
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    rid = body["id"]
    # Decision-only: the amount is folded into the payload, the request amount nulled
    # so the fine can never be paid OUT through disburse.
    assert body["amount_uzs"] is None
    assert body["payload"]["amount_uzs"] == "75000.00"

    ap = director.post(f"{REQ}{rid}/approve/", {"note": "fair"}, format="json")
    assert ap.status_code == 200, ap.content
    assert ap.json()["data"]["status"] == "approved"
    invoice_id = ap.json()["data"]["payload"]["invoice_id"]
    assert invoice_id  # audit link stamped back

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Invoice, InvoiceLine

        inv = Invoice.objects.get(pk=invoice_id)
        assert inv.student_id == sid
        assert inv.status == Invoice.Status.ISSUED
        assert inv.total_uzs == Decimal("75000.00")
        lines = list(InvoiceLine.objects.filter(invoice=inv))
        assert len(lines) == 1
        assert lines[0].line_type == InvoiceLine.LineType.PENALTY
        assert lines[0].amount_uzs == Decimal("75000.00")
        assert lines[0].description == "Repeated lateness"


def test_fine_ignores_a_standing_discount(tenant_a, as_role):
    """A scholarship must not shrink a punishment — the penalty invoice bills the
    full fine, with no discount line, even when the student has a standing discount."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        Discount.objects.create(
            student_id=sid,
            discount_type=Discount.DiscountType.MANUAL,
            percent=Decimal("50"),
            is_active=True,
        )

    rid = _raise_fine(teacher, sid, amount="60000").json()["data"]["id"]
    inv_id = director.post(f"{REQ}{rid}/approve/", {}, format="json").json()["data"]["payload"]["invoice_id"]

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Invoice, InvoiceLine

        inv = Invoice.objects.get(pk=inv_id)
        assert inv.total_uzs == Decimal("60000.00")  # NOT halved by the 50% discount
        assert not InvoiceLine.objects.filter(invoice=inv, line_type="discount").exists()


def test_fine_cannot_be_disbursed(tenant_a, as_role):
    """A fine collects money IN (a charge) — it is never paid OUT. With no amount on
    the request, disburse is refused."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)  # director also holds approvals:disburse
    sid = _student_id(tenant_a)
    rid = _raise_fine(teacher, sid).json()["data"]["id"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import PaymentMethod

        pm = PaymentMethod.objects.create(name="Cash", slug="cash")

    resp = director.post(f"{REQ}{rid}/disburse/", {"payment_method": pm.id}, format="json")
    assert resp.status_code == 422
    assert resp.json()["code"] == "approval_no_amount"


def test_fine_requires_valid_student(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    r = teacher.post(
        REQ,
        {"kind": "fine", "title": "x", "amount_uzs": "1000", "payload": {"reason": "no student"}},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "fine_student_required"


def test_fine_requires_an_amount(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    r = teacher.post(REQ, {"kind": "fine", "title": "x", "payload": {"student_id": sid}}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] == "fine_amount_required"


def test_fine_amount_overflow_rejected_at_the_gate(tenant_a):
    """NUMERIC(18,2) -> at most 16 integer digits; reject as a clean 400 at the
    service gate, not a DB 500 at issue time."""
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        sid = StudentProfileFactory.create().id
        with pytest.raises(ValidationException) as exc:
            services.create_request(
                kind="fine", title="x", amount_uzs=Decimal("1e16"), payload={"student_id": sid}
            )
        assert exc.value.code == "fine_amount_range"


def test_fine_amount_nan_or_infinity_rejected_at_the_gate(tenant_a):
    """A direct service caller passing a non-finite Decimal must get a clean 400, not
    a 500 from the unordered NaN comparison (never-raise DNA, defence in depth — the
    HTTP path is already guarded by the serializer's DecimalField)."""
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        sid = StudentProfileFactory.create().id
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(ValidationException) as exc:
                services.create_request(kind="fine", title="x", amount_uzs=bad, payload={"student_id": sid})
            assert exc.value.code in ("fine_amount_invalid", "fine_amount_range")


def test_fine_amount_that_rounds_up_to_overflow_rejected(tenant_a):
    """A value < 1e16 that ROUNDS UP to 1e16 at NUMERIC(18,2) would 500 at issue time;
    the gate re-checks the post-quantize value and rejects it as a clean 400."""
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        sid = StudentProfileFactory.create().id
        with pytest.raises(ValidationException) as exc:
            services.create_request(
                kind="fine",
                title="x",
                amount_uzs=Decimal("9999999999999999.999"),
                payload={"student_id": sid},
            )
        assert exc.value.code == "fine_amount_range"


def test_cannot_approve_own_fine(tenant_a, as_role):
    """Maker-checker: a director holding both write + approve may not sign off their
    own fine — and the penalty invoice must not be issued."""
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = _raise_fine(director, sid).json()["data"]["id"]

    resp = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "self_approval"

    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.finance.models import Invoice

        assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.Status.PENDING
        assert not Invoice.objects.filter(student_id=sid).exists()  # no effect fired


def test_rejecting_approved_fine_voids_the_invoice(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = _raise_fine(teacher, sid).json()["data"]["id"]
    inv_id = director.post(f"{REQ}{rid}/approve/", {}, format="json").json()["data"]["payload"]["invoice_id"]

    rej = director.post(f"{REQ}{rid}/reject/", {"note": "overturned on appeal"}, format="json")
    assert rej.status_code == 200
    assert rej.json()["data"]["status"] == "rejected"
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Invoice

        assert Invoice.objects.get(pk=inv_id).status == Invoice.Status.VOID  # no longer owed


def test_cannot_reject_a_fine_the_student_already_paid(tenant_a, as_role):
    """Anti-fraud: once money has moved against the fine, you cannot silently un-bill
    it — the reject is refused (409) and rolls back, forcing the refund flow."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = _raise_fine(teacher, sid).json()["data"]["id"]
    inv_id = director.post(f"{REQ}{rid}/approve/", {}, format="json").json()["data"]["payload"]["invoice_id"]

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import PaymentAllocation

        PaymentAllocation.objects.create(invoice_id=inv_id, payment_id=1, amount_uzs=Decimal("50000.00"))

    rej = director.post(f"{REQ}{rid}/reject/", {"note": "too late"}, format="json")
    assert rej.status_code == 409
    assert rej.json()["code"] == "invoice_has_payments"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.finance.models import Invoice

        # reject rolled back: request still approved, invoice still live (collectible).
        assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.Status.APPROVED
        assert Invoice.objects.get(pk=inv_id).status != Invoice.Status.VOID


def test_fine_student_deleted_before_approve(tenant_a, as_role):
    """The existence guard at approve time rolls the whole approval back (422,
    request stays pending, no orphan invoice)."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = _raise_fine(teacher, sid).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        from apps.students.models import StudentProfile

        StudentProfile.objects.filter(pk=sid).delete()

    resp = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert resp.status_code == 422
    assert resp.json()["code"] == "fine_student_missing"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest

        assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.Status.PENDING


def _penalty(tenant, student_id, *, points=3):
    from apps.compliance.models import Penalty
    from apps.students.models import StudentProfile

    with schema_context(tenant.schema_name):
        student = StudentProfile.objects.get(pk=student_id)
        return Penalty.objects.create(student=student, points=points, reason="late", branch=student.branch).id


def test_fine_can_cite_a_penalty_on_the_same_student(tenant_a, as_role):
    """F24-1: a fine may link the rule breach (a student demerit) it escalates from — the
    audit trail from discipline to money."""
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    pid = _penalty(tenant_a, sid)
    r = teacher.post(
        REQ,
        {
            "kind": "fine",
            "title": "x",
            "amount_uzs": "10000",
            "payload": {"student_id": sid, "penalty_id": pid},
        },
        format="json",
    )
    assert r.status_code == 201, r.content
    assert r.json()["data"]["payload"]["penalty_id"] == pid  # the audit link is stored


def test_fine_cannot_cite_another_students_penalty(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    other_pid = _penalty(tenant_a, _student_id(tenant_a))  # a penalty on a DIFFERENT student
    r = teacher.post(
        REQ,
        {
            "kind": "fine",
            "title": "x",
            "amount_uzs": "10000",
            "payload": {"student_id": sid, "penalty_id": other_pid},
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "fine_penalty_invalid"


def test_fine_with_a_nonexistent_penalty_is_rejected(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    r = teacher.post(
        REQ,
        {
            "kind": "fine",
            "title": "x",
            "amount_uzs": "10000",
            "payload": {"student_id": sid, "penalty_id": 999999},
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "fine_penalty_invalid"
