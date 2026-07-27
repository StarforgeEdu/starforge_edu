"""A-1 money feature — the `payment_delay` KIND of the Approvals engine.

A late-payment grace must be granted by someone with authority, not whispered.
Approving a `payment_delay` request pushes the target invoice's due date later
(a decision-only effect — no cash moves), and un-overdues a bill whose new
deadline is in the future. Rejecting an already-approved delay restores the
prior due date. Dignity (a sanctioned extension, not a black mark) plus
accountability (no untracked favours; full before/after audit trail).

Dates are computed relative to `today` so the suite is stable whenever it runs
(a payment delay must land today-or-later, which would break hardcoded dates)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.approvals.models import ApprovalRequest
from apps.finance.models import Invoice
from apps.finance.tests.factories import InvoiceFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

REQ = "/api/v1/approvals/requests/"


def _invoice(tenant, **kwargs) -> int:
    with schema_context(tenant.schema_name):
        return InvoiceFactory.create(**kwargs).id


def test_approving_payment_delay_extends_due_date(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    today = timezone.now().date()
    due = today + timedelta(days=5)
    new_due = today + timedelta(days=40)
    inv_id = _invoice(tenant_a, due_date=due, status=Invoice.Status.ISSUED)

    r = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "Grace",
            "amount_uzs": "500.00",  # ignored: a delay never disburses
            "payload": {"invoice_id": inv_id, "new_due_date": new_due.isoformat()},
        },
        format="json",
    )
    assert r.status_code == 201, r.content
    body = r.json()["data"]
    assert body["amount_uzs"] is None  # decision-only

    ap = director.post(f"{REQ}{body['id']}/approve/", {}, format="json")
    assert ap.status_code == 200, ap.content
    payload = ap.json()["data"]["payload"]
    assert payload["applied_due_date"] == new_due.isoformat()
    assert payload["invoice_status"] == "issued"

    with schema_context(tenant_a.schema_name):
        inv = Invoice.objects.get(pk=inv_id)
        assert inv.due_date == new_due
        assert inv.status == Invoice.Status.ISSUED


def test_payment_delay_unoverdues_invoice(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    today = timezone.now().date()
    new_due = today + timedelta(days=60)
    # Already tipped OVERDUE on an old due date; a future extension rescues it.
    inv_id = _invoice(tenant_a, due_date=today - timedelta(days=20), status=Invoice.Status.OVERDUE)

    rid = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "Extend",
            "payload": {"invoice_id": inv_id, "new_due_date": new_due.isoformat()},
        },
        format="json",
    ).json()["data"]["id"]
    ap = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert ap.status_code == 200, ap.content

    with schema_context(tenant_a.schema_name):
        inv = Invoice.objects.get(pk=inv_id)
        assert inv.due_date == new_due
        assert inv.status == Invoice.Status.ISSUED  # left the dunning queue


def test_payment_delay_partially_paid_invoice_unoverdues_to_partially_paid(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    today = timezone.now().date()
    new_due = today + timedelta(days=60)
    with schema_context(tenant_a.schema_name):
        from apps.finance.models import PaymentAllocation

        inv = InvoiceFactory.create(
            due_date=today - timedelta(days=20),
            status=Invoice.Status.OVERDUE,
            total_uzs=Decimal("1000000.00"),
        )
        # part-paid: allocated > 0 but < total
        PaymentAllocation.objects.create(invoice=inv, payment_id=1, amount_uzs=Decimal("400000.00"))
        inv_id = inv.id

    rid = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "Extend",
            "payload": {"invoice_id": inv_id, "new_due_date": new_due.isoformat()},
        },
        format="json",
    ).json()["data"]["id"]
    ap = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert ap.status_code == 200, ap.content
    assert ap.json()["data"]["payload"]["invoice_status"] == "partially_paid"

    with schema_context(tenant_a.schema_name):
        assert Invoice.objects.get(pk=inv_id).status == Invoice.Status.PARTIALLY_PAID


def test_rejecting_approved_delay_restores_due_date(tenant_a, as_role):
    """Overturning an approved delay puts the due date back and re-flags OVERDUE
    when the restored date is again in the past — a rejected grace must not stick."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    today = timezone.now().date()
    original_due = today - timedelta(days=20)
    new_due = today + timedelta(days=60)
    inv_id = _invoice(tenant_a, due_date=original_due, status=Invoice.Status.OVERDUE)

    rid = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "Extend",
            "payload": {"invoice_id": inv_id, "new_due_date": new_due.isoformat()},
        },
        format="json",
    ).json()["data"]["id"]
    director.post(f"{REQ}{rid}/approve/", {}, format="json")
    with schema_context(tenant_a.schema_name):
        inv = Invoice.objects.get(pk=inv_id)
        assert inv.due_date == new_due
        assert inv.status == Invoice.Status.ISSUED

    rej = director.post(f"{REQ}{rid}/reject/", {"note": "reconsidered"}, format="json")
    assert rej.status_code == 200
    assert rej.json()["data"]["status"] == "rejected"
    with schema_context(tenant_a.schema_name):
        inv = Invoice.objects.get(pk=inv_id)
        assert inv.due_date == original_due  # restored
        assert inv.status == Invoice.Status.OVERDUE  # past again -> back in dunning


def test_payment_delay_requires_valid_open_invoice(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    today = timezone.now().date()
    new_due = (today + timedelta(days=30)).isoformat()
    # missing invoice_id
    bad = teacher.post(
        REQ,
        {"kind": "payment_delay", "title": "x", "payload": {"new_due_date": new_due}},
        format="json",
    )
    assert bad.status_code == 400
    assert bad.json()["code"] == "payment_delay_invoice_required"

    # a paid invoice is not open
    paid_id = _invoice(tenant_a, status=Invoice.Status.PAID)
    closed = teacher.post(
        REQ,
        {"kind": "payment_delay", "title": "x", "payload": {"invoice_id": paid_id, "new_due_date": new_due}},
        format="json",
    )
    assert closed.status_code == 400
    assert closed.json()["code"] == "payment_delay_invoice_not_open"


def test_payment_delay_must_move_date_later(tenant_a, as_role):
    teacher, _ = as_role(Role.TEACHER)
    today = timezone.now().date()
    due = today + timedelta(days=5)
    inv_id = _invoice(tenant_a, due_date=due, status=Invoice.Status.ISSUED)
    r = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "x",
            "payload": {"invoice_id": inv_id, "new_due_date": today.isoformat()},  # earlier than due
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "payment_delay_not_later"


def test_payment_delay_into_the_past_rejected(tenant_a, as_role):
    """Later than the current due date but still before today = a meaningless
    no-op grace; reject it at the gate."""
    teacher, _ = as_role(Role.TEACHER)
    today = timezone.now().date()
    inv_id = _invoice(tenant_a, due_date=today - timedelta(days=20), status=Invoice.Status.OVERDUE)
    r = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "x",
            "payload": {"invoice_id": inv_id, "new_due_date": (today - timedelta(days=5)).isoformat()},
        },
        format="json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "payment_delay_in_past"


def test_payment_delay_revalidated_at_approve(tenant_a, as_role):
    """The invoice can change between request and decision: if it is voided in
    the meantime, approving rolls back atomically (422) and the request stays
    pending — never an invoice mutated for a no-longer-valid target."""
    teacher, _ = as_role(Role.TEACHER)
    director, _ = as_role(Role.DIRECTOR)
    today = timezone.now().date()
    inv_id = _invoice(tenant_a, due_date=today + timedelta(days=5), status=Invoice.Status.ISSUED)
    rid = teacher.post(
        REQ,
        {
            "kind": "payment_delay",
            "title": "x",
            "payload": {"invoice_id": inv_id, "new_due_date": (today + timedelta(days=40)).isoformat()},
        },
        format="json",
    ).json()["data"]["id"]

    with schema_context(tenant_a.schema_name):
        Invoice.objects.filter(pk=inv_id).update(status=Invoice.Status.VOID)

    ap = director.post(f"{REQ}{rid}/approve/", {}, format="json")
    assert ap.status_code == 422
    assert ap.json()["code"] == "invoice_not_open"

    with schema_context(tenant_a.schema_name):
        assert ApprovalRequest.objects.get(pk=rid).status == ApprovalRequest.Status.PENDING
