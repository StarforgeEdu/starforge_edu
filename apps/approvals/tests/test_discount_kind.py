"""A-1 money feature — the `discount` KIND of the Approvals engine.

A discount needs sign-off (anti-fraud DNA: no silently-applied price cuts). It is
a decision-only request: approving it materializes a standing finance.Discount for
the student, which billing then auto-applies as a negative invoice line. The
approval row is the permanent paper trail (who asked, who approved, why)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

REQ = "/api/v1/approvals/requests/"


def _student_id(tenant) -> int:
    with schema_context(tenant.schema_name):
        return StudentProfileFactory.create().id


def test_approving_discount_request_materializes_discount(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, director_user = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)

    r = teacher.post(
        REQ,
        {
            "kind": "discount",
            "title": "Scholarship for top student",
            "amount_uzs": "999.00",  # ignored: a discount never disburses
            "payload": {"student_id": sid, "discount_type": "scholarship", "percent": "20"},
        },
        format="json",
    )
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    rid = body["id"]
    assert body["amount_uzs"] is None  # decision-only — amount dropped
    assert body["payload"]["percent"] == "20.00"  # normalized to NUMERIC(5,2) scale

    ap = director.post(f"{REQ}{rid}/approve/", {"note": "earned it"}, format="json")
    assert ap.status_code == 200, ap.content
    assert ap.json()["data"]["status"] == "approved"
    discount_id = ap.json()["data"]["payload"]["discount_id"]
    assert discount_id  # audit link stamped back

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        d = Discount.objects.get(pk=discount_id)
        assert d.student_id == sid
        assert d.percent == Decimal("20")
        assert d.fixed_amount_uzs is None
        assert d.discount_type == "scholarship"
        assert d.approved_by_id == director_user.id
        assert d.is_active is True


def test_discount_request_fixed_amount(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)

    rid = teacher.post(
        REQ,
        {
            "kind": "discount",
            "title": "Hardship",
            "payload": {"student_id": sid, "fixed_amount_uzs": "150000"},
        },
        format="json",
    ).json()["data"]["id"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")

    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        d = Discount.objects.get(student_id=sid)
        assert d.fixed_amount_uzs == Decimal("150000")
        assert d.percent is None
        assert d.discount_type == "manual"  # defaulted


def test_discount_request_requires_valid_student(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    r = teacher.post(
        REQ,
        {"kind": "discount", "title": "x", "payload": {"percent": "10"}},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "discount_student_required"


def test_discount_request_amount_is_xor(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    # both set -> rejected
    both = teacher.post(
        REQ,
        {
            "kind": "discount",
            "title": "x",
            "payload": {"student_id": sid, "percent": "10", "fixed_amount_uzs": "1000"},
        },
        format="json",
    )
    assert both.status_code == 400
    assert both.json()["code"] == "discount_amount_xor"
    # neither set -> also rejected
    neither = teacher.post(
        REQ, {"kind": "discount", "title": "x", "payload": {"student_id": sid}}, format="json"
    )
    assert neither.status_code == 400
    assert neither.json()["code"] == "discount_amount_xor"


def test_discount_percent_out_of_range(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    r = teacher.post(
        REQ,
        {"kind": "discount", "title": "x", "payload": {"student_id": sid, "percent": "150"}},
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "discount_percent_range"


def test_discount_type_invalid_rejected(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    r = teacher.post(
        REQ,
        {
            "kind": "discount",
            "title": "x",
            "payload": {"student_id": sid, "percent": "10", "discount_type": "bogus"},
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "discount_type_invalid"


def test_fixed_amount_overflow_rejected(tenant_a, as_role):
    # NUMERIC(18,2) -> at most 16 integer digits; reject as a clean 400, not a DB 500.
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    r = teacher.post(
        REQ,
        {
            "kind": "discount",
            "title": "x",
            "payload": {"student_id": sid, "fixed_amount_uzs": "100000000000000000"},  # 1e17
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "discount_fixed_range"


def test_discount_nan_amounts_rejected_not_500(tenant_a, as_role):
    """A non-finite Decimal in the freeform payload is unordered — the range
    comparison would raise InvalidOperation (a 500). It must be a clean 400."""
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    for field, code in (
        ("percent", "discount_percent_invalid"),
        ("fixed_amount_uzs", "discount_fixed_invalid"),
    ):
        r = teacher.post(
            REQ,
            {"kind": "discount", "title": "x", "payload": {"student_id": sid, field: "NaN"}},
            format="json",
        )
        assert r.status_code == 400, (field, r.content)
        assert r.json()["code"] == code


def test_discount_fixed_amount_that_rounds_up_to_overflow_rejected(tenant_a, as_role):
    """A value < 1e16 that ROUNDS UP to 1e16 at NUMERIC(18,2) would 500 at insert;
    the post-quantize re-check rejects it as a clean 400."""
    teacher, _ = as_role(Role.TEACHER)
    sid = _student_id(tenant_a)
    r = teacher.post(
        REQ,
        {
            "kind": "discount",
            "title": "x",
            "payload": {"student_id": sid, "fixed_amount_uzs": "9999999999999999.999"},
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "discount_fixed_range"


def test_percent_quantized_to_two_places(tenant_a, as_role):
    """The audited payload must equal the discount that actually bills the student
    (Postgres NUMERIC(5,2) would otherwise silently round on insert)."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    body = teacher.post(
        REQ,
        {"kind": "discount", "title": "x", "payload": {"student_id": sid, "percent": "33.333"}},
        format="json",
    ).json()["data"]
    assert body["payload"]["percent"] == "33.33"  # normalized at the gate

    director.post(f"{REQ}{body['id']}/approve/", {}, format="json")
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(student_id=sid).percent == Decimal("33.33")


def test_cannot_approve_own_request(tenant_a, as_role):
    """Maker-checker: a user holding both approvals:write and approvals:approve
    (e.g. director) may not sign off their own request — the effect must not fire."""
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = director.post(
        REQ,
        {"kind": "discount", "title": "self", "payload": {"student_id": sid, "percent": "10"}},
        format="json",
    ).json()["data"]["id"]

    resp = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert resp.status_code == 403
    assert resp.json()["code"] == "self_approval"

    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.finance.models import Discount

        assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.Status.PENDING
        assert not Discount.objects.filter(student_id=sid).exists()  # no effect fired


def test_rejecting_approved_discount_deactivates_it(tenant_a, as_role):
    """A rejected price cut must stop cutting prices: reject-after-approve
    deactivates the standing Discount so billing no longer applies it."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = teacher.post(
        REQ,
        {"kind": "discount", "title": "x", "payload": {"student_id": sid, "percent": "15"}},
        format="json",
    ).json()["data"]["id"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(student_id=sid).is_active is True

    rej = director.post(f"{REQ}{rid}/reject/", {"note": "reconsidered"}, format="json")
    assert rej.status_code == 200
    assert rej.json()["data"]["status"] == "rejected"
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import Discount

        assert Discount.objects.get(student_id=sid).is_active is False


def test_discount_student_deleted_before_approve(tenant_a, as_role):
    """The existence guard at approve time rolls the whole approval back (422,
    request stays pending, no orphan Discount)."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    sid = _student_id(tenant_a)
    rid = teacher.post(
        REQ,
        {"kind": "discount", "title": "x", "payload": {"student_id": sid, "percent": "15"}},
        format="json",
    ).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        from apps.students.models import StudentProfile

        StudentProfile.objects.filter(pk=sid).delete()

    resp = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert resp.status_code == 422
    assert resp.json()["code"] == "discount_student_missing"
    with schema_context(tenant_a.schema_name):
        from apps.approvals.models import ApprovalRequest
        from apps.finance.models import Discount

        assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.Status.PENDING
        assert not Discount.objects.filter(student_id=sid).exists()
