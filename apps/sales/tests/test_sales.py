"""#8 — book/material cash sales: a sale writes an immutable money-IN ledger row; a
refund writes a compensating money-OUT row (the ledger is never mutated)."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

SALES = "/api/v1/sales/"
LEDGER = "/api/v1/approvals/ledger/"


def _setup(tenant, user_in, as_user):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory.create()
        student = StudentProfileFactory.create(branch=branch)
        method = PaymentMethod.objects.create(name="Cash", slug="cash")
    return {
        "branch": branch,
        "student": student,
        "method": method.id,
        "cashier": as_user(tenant, user_in(tenant, roles=[Role.CASHIER], branch=branch)),
        "registrar": as_user(tenant, user_in(tenant, roles=[Role.REGISTRAR], branch=branch)),
    }


def _sale_body(s, **over):
    body = {
        "item": "Course book",
        "quantity": 2,
        "unit_price_uzs": "75000.00",
        "student": s["student"].id,
        "payment_method": s["method"],
    }
    body.update(over)
    return body


def test_record_sale_writes_money_in_ledger(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["cashier"].post(SALES, _sale_body(s), format="json")
    assert r.status_code == 201, r.content
    assert r.json()["data"]["status"] == "completed"
    assert r.json()["data"]["amount_uzs"] == "150000.00"  # 2 x 75000
    assert r.json()["data"]["ledger_entry"] is not None

    entries = s["cashier"].get(LEDGER).json()["data"]
    assert any(
        e["entry_type"] == "book_sale" and e["direction"] == "in" and e["amount_uzs"] == "150000.00"
        for e in entries
    )


def test_refund_writes_compensating_out_row(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    sid = s["cashier"].post(SALES, _sale_body(s), format="json").json()["data"]["id"]

    refunded = s["cashier"].post(f"{SALES}{sid}/refund/", {"reason": "wrong book"}, format="json")
    assert refunded.status_code == 200, refunded.content
    assert refunded.json()["data"]["status"] == "refunded"
    assert refunded.json()["data"]["refund_ledger_entry"] is not None
    # a refunded sale can't be refunded again
    assert s["cashier"].post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 422

    # the OUT row COMPENSATES the IN row (nets to zero); the original IN row is preserved
    entries = s["cashier"].get(LEDGER).json()["data"]
    ins = [e for e in entries if e["entry_type"] == "book_sale"]
    outs = [e for e in entries if e["entry_type"] == "book_sale_refund"]
    assert len(ins) == 1
    assert len(outs) == 1
    assert outs[0]["direction"] == "out"
    assert ins[0]["amount_uzs"] == outs[0]["amount_uzs"] == "150000.00"


def test_refund_is_branch_scoped(tenant_a, user_in, as_user, as_role):
    from apps.org.tests.factories import BranchFactory

    s = _setup(tenant_a, user_in, as_user)
    sid = s["cashier"].post(SALES, _sale_body(s), format="json").json()["data"]["id"]
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
    other_cashier = as_user(tenant_a, user_in(tenant_a, roles=[Role.CASHIER], branch=other_branch))
    # a cashier at another branch can neither see nor refund this sale -> 404 (not 422/403)
    assert other_cashier.get(f"{SALES}{sid}/").status_code == 404
    assert other_cashier.post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 404
    # a director (any branch) can refund
    director, _ = as_role(Role.DIRECTOR)
    assert director.post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 200


def test_staff_list_is_branch_scoped(tenant_a, user_in, as_user):
    from apps.finance.models import PaymentMethod
    from apps.org.tests.factories import BranchFactory
    from apps.sales.models import Sale
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    s["cashier"].post(SALES, _sale_body(s), format="json")  # a sale in the cashier's branch
    with schema_context(tenant_a.schema_name):  # ...and one in another branch
        other_branch = BranchFactory.create()
        other_student = StudentProfileFactory.create(branch=other_branch)
        method = PaymentMethod.objects.create(name="Card", slug="card")
        Sale.objects.create(
            item="x",
            quantity=1,
            unit_price_uzs="10.00",
            amount_uzs="10.00",
            student=other_student,
            branch=other_branch,
            payment_method=method,
        )

    body = s["cashier"].get(SALES).json()
    assert body["pagination"]["total"] == 1  # the cashier sees only their own branch's till
    assert body["data"][0]["branch"] == s["branch"].id


def test_cannot_sell_to_another_branchs_student(tenant_a, user_in, as_user):
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    s = _setup(tenant_a, user_in, as_user)
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
        other_student = StudentProfileFactory.create(branch=other_branch)
    r = s["cashier"].post(SALES, _sale_body(s, student=other_student.id), format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "branch_out_of_scope"


def test_refund_requires_refund_permission(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # reception can ring up a sale (sale:write) but not refund (no sale:refund) — SoD
    sid = s["registrar"].post(SALES, _sale_body(s), format="json").json()["data"]["id"]
    assert s["registrar"].post(f"{SALES}{sid}/refund/", {}, format="json").status_code == 403


def test_invalid_payment_method_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    r = s["cashier"].post(SALES, _sale_body(s, payment_method=999999), format="json")
    assert r.status_code == 422
    assert r.json()["code"] == "payment_method_invalid"


def test_quantity_is_capped(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # an absurd quantity is a clean 400, not a DB-overflow 500
    r = s["cashier"].post(SALES, _sale_body(s, quantity=3_000_000_000), format="json")
    assert r.status_code == 400


def test_sub_cent_unit_price_rejected(tenant_a, user_in, as_user):
    s = _setup(tenant_a, user_in, as_user)
    # >2 decimal places would be quantized to 0.01 on the NUMERIC(18,2) column while the
    # ledgered amount is derived from the full-precision input (0.014 x qty) -> the line
    # item would not reconcile with the immutable money-IN row. The old DecimalField 400'd
    # this via validate_precision; the layered path must too.
    r = s["cashier"].post(SALES, _sale_body(s, unit_price_uzs="0.014"), format="json")
    assert r.status_code == 400


def test_non_finance_roles_cannot_see_the_till(tenant_a, as_role):
    # the till isn't family-facing: teacher / student / parent have no sale:read
    for role in (Role.TEACHER, Role.STUDENT, Role.PARENT):
        client, _ = as_role(role)
        assert client.get(SALES).status_code == 403
