"""#15 — procurement / purchase orders: an itemised `kind="procurement"` of the A-1
engine. Raise a PO (line items → a request totalling them) → approve → cashier
disburses to the supplier → immutable ledger row. Decision lives in /approvals/."""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from core.permissions import Role

pytestmark = pytest.mark.django_db

PO = "/api/v1/procurement/"
REQ = "/api/v1/approvals/requests/"
LEDGER = "/api/v1/approvals/ledger/"

ITEMS = [
    {"description": "Whiteboard markers", "quantity": "2", "unit_price_uzs": "1500.00"},
    {"description": "A4 paper (ream)", "quantity": "3", "unit_price_uzs": "1000.00"},
]  # total = 3000 + 3000 = 6000.00


def _payment_method(tenant) -> int:
    with schema_context(tenant.schema_name):
        from apps.finance.models import PaymentMethod

        return PaymentMethod.objects.create(name="Cash", slug="cash").id


def test_create_po_then_approve_disburse_writes_ledger(tenant_a, as_role):
    registrar, _ = as_role(Role.REGISTRAR)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    created = registrar.post(
        PO, {"title": "Classroom supplies", "supplier": "Acme Co", "items": ITEMS}, format="json"
    )
    assert created.status_code == 201, created.content
    body = created.json()["data"]
    assert body["status"] == "pending"
    assert body["amount_uzs"] == "6000.00"  # totalled from the line items
    assert len(body["items"]) == 2
    assert body["items"][0]["line_total_uzs"] == "3000.00"
    rid = body["request"]

    assert director.post(f"{REQ}{rid}/approve/", {"note": "ok"}, format="json").status_code == 200
    dis = cashier.post(f"{REQ}{rid}/disburse/", {"payment_method": method_id}, format="json")
    assert dis.status_code == 200, dis.content
    assert dis.json()["data"]["status"] == "disbursed"

    entries = cashier.get(LEDGER).json()["data"]
    paid = next(e for e in entries if e["entry_type"] == "procurement" and e["direction"] == "out")
    assert paid["amount_uzs"] == "6000.00"
    assert paid["party_label"] == "Acme Co"  # the supplier is named, not the requester


def test_po_requires_at_least_one_item(tenant_a, as_role):
    registrar, _ = as_role(Role.REGISTRAR)
    r = registrar.post(PO, {"title": "Empty", "supplier": "Acme", "items": []}, format="json")
    assert r.status_code == 400  # serializer allow_empty=False


def test_po_total_must_be_positive(tenant_a, as_role):
    registrar, _ = as_role(Role.REGISTRAR)
    # a single free line item totals zero — rejected (a procurement moves money)
    r = registrar.post(
        PO,
        {
            "title": "Freebie",
            "supplier": "Acme",
            "items": [{"description": "x", "quantity": "1", "unit_price_uzs": "0"}],
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "po_total_positive"


def test_requester_sees_own_po_handler_sees_all(tenant_a, as_role):
    registrar_a, _ = as_role(Role.REGISTRAR)
    registrar_b, _ = as_role(Role.REGISTRAR)  # a different requester (no approvals:approve)
    director, _ = as_role(Role.DIRECTOR)
    registrar_a.post(PO, {"title": "Mine", "supplier": "Acme", "items": ITEMS}, format="json")

    assert registrar_a.get(PO).json()["pagination"]["total"] == 1  # requester sees own
    assert registrar_b.get(PO).json()["pagination"]["total"] == 0  # another requester sees none of it
    assert director.get(PO).json()["pagination"]["total"] == 1  # handler sees all


def test_po_total_too_large_is_rejected(tenant_a, as_role):
    registrar, _ = as_role(Role.REGISTRAR)
    # one near-max line x qty 2 overflows NUMERIC(18,2) - caught as a clean 400
    r = registrar.post(
        PO,
        {
            "title": "Huge",
            "supplier": "Acme",
            "items": [{"description": "x", "quantity": "2", "unit_price_uzs": "9999999999999999.99"}],
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "po_total_too_large"


def test_negative_unit_price_rejected(tenant_a, as_role):
    registrar, _ = as_role(Role.REGISTRAR)
    r = registrar.post(
        PO,
        {
            "title": "x",
            "supplier": "Acme",
            "items": [{"description": "d", "quantity": "1", "unit_price_uzs": "-5"}],
        },
        format="json",
    )
    assert r.status_code == 400  # serializer min_value=0 on unit_price


def test_cannot_raise_po_for_another_branch(tenant_a, as_role):
    """A branch-scoped requester can't book spend against a branch they don't belong to."""
    from apps.org.tests.factories import BranchFactory

    registrar, _ = as_role(Role.REGISTRAR)  # auto-gets a membership in its own branch
    with schema_context(tenant_a.schema_name):
        other_branch = BranchFactory.create()
    r = registrar.post(
        PO, {"title": "x", "supplier": "Acme", "branch": other_branch.id, "items": ITEMS}, format="json"
    )
    assert r.status_code == 403
    assert r.json()["code"] == "branch_out_of_scope"


def test_disburse_cannot_override_supplier_or_direction(tenant_a, as_role):
    """The ledger row for a PO is pinned to the approved supplier + money-OUT; a
    cashier cannot substitute the payee or flip the sign at disburse time."""
    registrar, _ = as_role(Role.REGISTRAR)
    director, _ = as_role(Role.DIRECTOR)
    cashier, _ = as_role(Role.CASHIER)
    method_id = _payment_method(tenant_a)

    rid = registrar.post(
        PO, {"title": "Supplies", "supplier": "Acme Co", "items": ITEMS}, format="json"
    ).json()["data"]["request"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")
    # the cashier tries to misname the payee and flip the direction
    cashier.post(
        f"{REQ}{rid}/disburse/",
        {"payment_method": method_id, "party_label": "Somebody Else", "direction": "in"},
        format="json",
    )
    entries = cashier.get(LEDGER).json()["data"]
    row = next(e for e in entries if e["entry_type"] == "procurement")
    assert row["party_label"] == "Acme Co"  # the approved supplier wins
    assert row["direction"] == "out"  # money OUT is forced


def test_fractional_line_totals_reconcile_to_amount(tenant_a, as_role):
    """The displayed line totals must sum exactly to amount_uzs even with fractional
    quantities (round-each-line-then-sum, not sum-then-round)."""
    registrar, _ = as_role(Role.REGISTRAR)
    items = [
        {"description": "a", "quantity": "1.5", "unit_price_uzs": "333.33"},
        {"description": "b", "quantity": "1.5", "unit_price_uzs": "333.33"},
    ]  # each line 499.995 → 500.00; total 1000.00
    body = registrar.post(PO, {"title": "Frac", "supplier": "Acme", "items": items}, format="json").json()[
        "data"
    ]
    assert [i["line_total_uzs"] for i in body["items"]] == ["500.00", "500.00"]
    assert body["amount_uzs"] == "1000.00"  # equals the sum of the shown line totals


def test_role_without_procurement_is_denied(tenant_a, as_role):
    student, _ = as_role(Role.STUDENT)
    assert student.get(PO).status_code == 403
