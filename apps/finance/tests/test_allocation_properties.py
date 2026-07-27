"""D3-F-5 — payment allocation rounding properties.

``apps.finance.services.allocate_payment(payment_id, amount_uzs, invoice_ids)``
(D3-A-4) splits a payment over the oldest-due invoices first with EXACT Decimal
accounting. Adversarial over awkward amounts (odd thirds, sub-tiyin boundary,
max-digits) we assert the invariants that a float implementation would violate:

  P1. ``sum(allocation.amount_uzs) == amount_uzs`` EXACTLY (no rounding loss).
  P2. No invoice is over-credited (each allocation <= that invoice's outstanding).
  P3. Every stored amount is a ``Decimal`` (not float) and quantized to 2 dp.
  P4. Invoice status flips issued -> partially_paid -> paid correctly.
  P5. Over-allocation (amount > total outstanding) raises ValidationException.

Lane A builds the service/models in parallel; lane imports are lazy. The
orchestrator runs this on Postgres after merge.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

# Awkward amounts x invoice splits that break naive float / even-division code.
# Each: (total_amount_uzs, [per-invoice issued totals]) where sum(issued) >= amount.
AWKWARD_CASES = [
    # 1,000,000.01 over three equal invoices — the classic non-divisible-by-3 cent.
    ("1000000.01", ["400000.00", "400000.00", "400000.00"]),
    # tiny single cent.
    ("0.01", ["100.00"]),
    # odd thirds: 100.00 / 3 = 33.33 + 33.33 + 33.34
    ("100.00", ["50.00", "50.00", "50.00"]),
    # exact full payment of two invoices.
    ("250000.00", ["100000.00", "150000.00"]),
    # partial — only fills the first invoice and part of the second.
    ("120000.00", ["100000.00", "100000.00"]),
    # max-digits boundary (18 digits, 2 dp): 9,999,999,999,999,999.99-ish, split.
    ("9999999999999.99", ["5000000000000.00", "5000000000000.00"]),
    # remainder lands as a sub-cent that must be absorbed, not dropped.
    ("0.03", ["0.01", "0.01", "0.01"]),
]


def _make_payment_and_invoices(center, *, amount_uzs, issued_totals):
    """Create one pending Payment and N issued invoices (oldest-due first by
    ascending due_date) in the tenant schema. Returns (payment, [invoice ids])."""
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.finance.models import Invoice
    from apps.org.tests.factories import BranchFactory
    from apps.payments.models import Payment
    from apps.students.tests.factories import StudentProfileFactory

    branch = BranchFactory()
    cohort = CohortFactory(branch=branch)
    student = StudentProfileFactory(branch=branch)
    CohortMembershipFactory(cohort=cohort, student=student)

    ids = []
    base_due = date(2026, 6, 1)
    for i, total in enumerate(issued_totals):
        inv = Invoice.objects.create(
            number=f"INV-2026-{i:06d}",
            student=student,
            cohort=cohort,
            status="issued",
            issue_date=date(2026, 5, 1),
            due_date=base_due + timedelta(days=i),  # ascending => oldest-due first
            currency="UZS",
            total_uzs=Decimal(total),
        )
        ids.append(inv.id)

    payment = Payment.objects.create(
        provider="cash",
        amount_uzs=Decimal(amount_uzs),
        status="completed",
        idempotency_key=f"alloc-test-{amount_uzs}",
        account_ref=(ids and f"INV-2026-{0:06d}") or "",
    )
    return payment, ids


@pytest.mark.parametrize(("amount_uzs", "issued_totals"), AWKWARD_CASES)
def test_allocation_sum_equals_amount_exactly(tenant_a, amount_uzs, issued_totals):
    from apps.finance import services
    from apps.finance.models import PaymentAllocation

    with schema_context(tenant_a.schema_name):
        payment, _inv_ids = _make_payment_and_invoices(
            tenant_a, amount_uzs=amount_uzs, issued_totals=issued_totals
        )
        services.allocate_payment(payment_id=payment.id, amount_uzs=Decimal(amount_uzs), invoice_ids=None)
        allocs = list(PaymentAllocation.objects.filter(payment_id=payment.id))
        # P1 — exact, no rounding loss.
        total_allocated = sum((a.amount_uzs for a in allocs), Decimal("0"))
        assert total_allocated == Decimal(amount_uzs)


@pytest.mark.parametrize(("amount_uzs", "issued_totals"), AWKWARD_CASES)
def test_no_invoice_over_credited(tenant_a, amount_uzs, issued_totals):
    from apps.finance import services
    from apps.finance.models import PaymentAllocation

    with schema_context(tenant_a.schema_name):
        payment, inv_ids = _make_payment_and_invoices(
            tenant_a, amount_uzs=amount_uzs, issued_totals=issued_totals
        )
        services.allocate_payment(payment_id=payment.id, amount_uzs=Decimal(amount_uzs), invoice_ids=None)
        per_invoice: dict[int, Decimal] = {}
        for a in PaymentAllocation.objects.filter(payment_id=payment.id):
            per_invoice[a.invoice_id] = per_invoice.get(a.invoice_id, Decimal("0")) + a.amount_uzs
        # P2 — never credit an invoice beyond its issued total.
        for inv_id, total in zip(inv_ids, issued_totals, strict=False):
            assert per_invoice.get(inv_id, Decimal("0")) <= Decimal(total)


@pytest.mark.parametrize(("amount_uzs", "issued_totals"), AWKWARD_CASES)
def test_allocations_are_decimal_two_places(tenant_a, amount_uzs, issued_totals):
    from apps.finance import services
    from apps.finance.models import PaymentAllocation

    with schema_context(tenant_a.schema_name):
        payment, _ = _make_payment_and_invoices(tenant_a, amount_uzs=amount_uzs, issued_totals=issued_totals)
        services.allocate_payment(payment_id=payment.id, amount_uzs=Decimal(amount_uzs), invoice_ids=None)
        for a in PaymentAllocation.objects.filter(payment_id=payment.id):
            # P3 — Decimal, not float, and quantized to 2dp (money column contract).
            assert isinstance(a.amount_uzs, Decimal)
            assert -a.amount_uzs.as_tuple().exponent <= 2
            assert a.amount_uzs > Decimal("0")  # PaymentAllocation: amount_uzs > 0


def test_status_flips_issued_partially_paid_paid(tenant_a):
    from apps.finance import services
    from apps.finance.models import Invoice
    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        payment, inv_ids = _make_payment_and_invoices(
            tenant_a, amount_uzs="150000.00", issued_totals=["100000.00", "100000.00"]
        )
        # Pay 150k across two 100k invoices: first -> paid, second -> partially_paid.
        services.allocate_payment(payment_id=payment.id, amount_uzs=Decimal("150000.00"), invoice_ids=None)
        first = Invoice.objects.get(id=inv_ids[0])
        second = Invoice.objects.get(id=inv_ids[1])
        assert first.status == "paid"
        assert second.status == "partially_paid"

        # Top the second up to fully paid with a fresh payment.
        topup = Payment.objects.create(
            provider="cash",
            amount_uzs=Decimal("50000.00"),
            status="completed",
            idempotency_key="alloc-topup",
            account_ref=second.number,
        )
        services.allocate_payment(
            payment_id=topup.id, amount_uzs=Decimal("50000.00"), invoice_ids=[second.id]
        )
        second.refresh_from_db()
        assert second.status == "paid"


def test_over_allocation_raises(tenant_a):
    from apps.finance import services
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        payment, inv_ids = _make_payment_and_invoices(
            tenant_a, amount_uzs="100000.00", issued_totals=["50000.00"]
        )
        # Allocating 100k onto a single 50k invoice over-credits it.
        with pytest.raises(ValidationException):
            services.allocate_payment(
                payment_id=payment.id,
                amount_uzs=Decimal("100000.00"),
                invoice_ids=[inv_ids[0]],
            )


def test_explicit_invoice_targeting_respects_order(tenant_a):
    """When invoice_ids is given, allocation targets exactly those invoices."""
    from apps.finance import services
    from apps.finance.models import PaymentAllocation

    with schema_context(tenant_a.schema_name):
        payment, inv_ids = _make_payment_and_invoices(
            tenant_a, amount_uzs="40000.00", issued_totals=["100000.00", "100000.00"]
        )
        # Only target the SECOND invoice.
        services.allocate_payment(
            payment_id=payment.id, amount_uzs=Decimal("40000.00"), invoice_ids=[inv_ids[1]]
        )
        allocs = list(PaymentAllocation.objects.filter(payment_id=payment.id))
        assert {a.invoice_id for a in allocs} == {inv_ids[1]}
        assert sum((a.amount_uzs for a in allocs), Decimal("0")) == Decimal("40000.00")
