"""Lane B service-level tests (D3-B-6..11) — the non-attack half of the matrix.

These exercise the payments services directly inside ``schema_context`` (the
webhook/attack surface lives in test_webhook_attacks / test_payme_spec):

- fiscalization task idempotency (D3-B-9) — a second run reuses the confirmed
  ``FiscalReceipt`` and the mock fiscal sign is deterministic;
- reconciliation math (D3-B-10) — paid vs allocated totals + the mismatch list;
- Click prepare/complete happy path (D3-B-2) drives a completed Payment +
  single allocation;
- refund flow (D3-B-8) rejects a refund on a non-completed Payment;
- ``payment_completed`` / ``payment_failed`` fire exactly once per transition
  (D3-B-11).

Lane code is imported lazily/locally; the orchestrator runs this on Postgres
after A..E merge (finance.Refund / allocate_payment land in Lane A).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.payments.tests import _helpers as helpers

pytestmark = pytest.mark.django_db

AMOUNT_UZS = "150000.00"


@pytest.fixture
def invoice_a(tenant_a):
    helpers.seed_provider_configs(tenant_a)
    inv = helpers.seed_open_invoice(tenant_a, number="INV-2026-000001", amount_uzs=AMOUNT_UZS)
    return tenant_a, inv


# --------------------------------------------------------------------------- #
# Fiscalization idempotency (D3-B-9) — mock determinism + short-circuit
# --------------------------------------------------------------------------- #
def test_fiscalize_payment_idempotent_and_deterministic(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from apps.payments.models import FiscalReceipt, Payment

    with schema_context(tenant_a.schema_name):
        payment, _ = services.get_or_create_payment(
            idempotency_key="fisc-1",
            provider="payme",
            amount_uzs=Decimal(AMOUNT_UZS),
            account_ref=inv.number,
            metadata={"invoice_id": inv.id, "student_id": inv.student_id},
        )
        # Drive it completed WITHOUT auto-allocation noise; fiscalize directly.
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.COMPLETED)

        first = services.fiscalize_payment_body(payment.pk)
        second = services.fiscalize_payment_body(payment.pk)

        assert first == second, "same payment must fiscalize to the same sign (mock determinism)"
        receipts = FiscalReceipt.objects.filter(payment=payment)
        assert receipts.count() == 1
        receipt = receipts.get()
        assert receipt.status == FiscalReceipt.Status.CONFIRMED
        assert receipt.fiscal_sign == first
        assert receipt.qr_url
        # attempts incremented exactly once — the confirmed receipt short-circuits.
        assert receipt.attempts == 1


def test_fiscalize_rejects_non_completed_payment(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from core.exceptions import UnprocessableEntity

    with schema_context(tenant_a.schema_name):
        payment, _ = services.get_or_create_payment(
            idempotency_key="fisc-pending",
            provider="payme",
            amount_uzs=Decimal(AMOUNT_UZS),
            account_ref=inv.number,
        )
        with pytest.raises(UnprocessableEntity):
            services.fiscalize_payment_body(payment.pk)


# --------------------------------------------------------------------------- #
# Click prepare/complete happy path (D3-B-2) + signal once (D3-B-11)
# --------------------------------------------------------------------------- #
def test_click_complete_completes_payment_and_allocates(invoice_a, django_capture_on_commit_callbacks):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from apps.payments.models import Payment
    from apps.payments.signals import payment_completed

    completed_signals: list[dict] = []
    # weak=False: a bare-lambda receiver has no strong ref and would be GC'd
    # before dispatch (Django drops dead weakrefs), making the listener silently
    # never fire. The production receiver (notifications) also connects weak=False.
    payment_completed.connect(
        lambda sender, **kwargs: completed_signals.append(kwargs),
        dispatch_uid="test.click.completed",
        weak=False,
    )
    try:
        with schema_context(tenant_a.schema_name):
            # execute=True runs the transaction.on_commit callbacks (signals) so
            # the emit-on-commit contract is exercised (mirrors attendance tests).
            with django_capture_on_commit_callbacks(execute=True):
                payment = services.process_click_complete(
                    payload={
                        "click_trans_id": "click-1",
                        "merchant_trans_id": inv.number,
                        "amount": AMOUNT_UZS,
                    },
                    invoice=inv,
                )
            payment.refresh_from_db()
            assert payment.status == Payment.Status.COMPLETED
            assert payment.provider_txn_id == "click-1"
            assert payment.paid_at is not None
            allocs = helpers.allocation_rows(tenant_a, payment_id=payment.pk)
            assert len(allocs) == 1
            assert allocs[0].amount_uzs == Decimal(AMOUNT_UZS)
            assert payment.allocation_status == Payment.Allocation.ALLOCATED
    finally:
        payment_completed.disconnect(dispatch_uid="test.click.completed")

    # exactly one payment_completed for the single transition
    assert len(completed_signals) == 1
    assert completed_signals[0]["payment_id"] == payment.pk
    assert completed_signals[0]["invoice_id"] == inv.id
    assert completed_signals[0]["amount_uzs"] == AMOUNT_UZS


def test_mark_payment_completed_twice_emits_one_signal(invoice_a, django_capture_on_commit_callbacks):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from apps.payments.signals import payment_completed

    fired: list[dict] = []
    # weak=False: see test_click_complete_completes_payment_and_allocates — a bare
    # lambda would be garbage-collected before the signal dispatches.
    payment_completed.connect(
        lambda sender, **kw: fired.append(kw), dispatch_uid="test.once.completed", weak=False
    )
    try:
        with schema_context(tenant_a.schema_name):
            payment, _ = services.get_or_create_payment(
                idempotency_key="once-1",
                provider="click",
                amount_uzs=Decimal(AMOUNT_UZS),
                account_ref=inv.number,
                metadata={"invoice_id": inv.id, "student_id": inv.student_id},
            )
            with django_capture_on_commit_callbacks(execute=True):
                services.mark_payment_completed(payment_id=payment.pk, provider_txn_id="t1")
            with django_capture_on_commit_callbacks(execute=True):
                # second transition is a no-op — no second signal registered
                services.mark_payment_completed(payment_id=payment.pk, provider_txn_id="t1")
    finally:
        payment_completed.disconnect(dispatch_uid="test.once.completed")
    assert len(fired) == 1, "completion signal must fire exactly once per transition"


# --------------------------------------------------------------------------- #
# Refund flow (D3-B-8)
# --------------------------------------------------------------------------- #
def test_refund_on_non_completed_payment_rejected(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from core.exceptions import UnprocessableEntity

    with schema_context(tenant_a.schema_name):
        payment, _ = services.get_or_create_payment(
            idempotency_key="refund-pending",
            provider="payme",
            amount_uzs=Decimal(AMOUNT_UZS),
            account_ref=inv.number,
            metadata={"invoice_id": inv.id, "student_id": inv.student_id},
        )
        with pytest.raises(UnprocessableEntity):
            services.refund_payment(payment_id=payment.pk, reason="oops")


def test_refund_completed_payment_drives_finance_refund(invoice_a):
    tenant_a, inv = invoice_a
    from apps.finance import selectors as finance_selectors
    from apps.finance.models import Invoice, Refund
    from apps.payments import services
    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        # complete + allocate first, so the invoice has a paid amount to reverse
        payment = services.process_click_complete(
            payload={
                "click_trans_id": "click-refund",
                "merchant_trans_id": inv.number,
                "amount": AMOUNT_UZS,
            },
            invoice=inv,
        )
        # precondition: the invoice is fully PAID and the balance is zero
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.PAID
        assert finance_selectors.outstanding_balance(inv.student_id) == Decimal("0.00")

        services.refund_payment(payment_id=payment.pk, reason="customer_request")
        payment.refresh_from_db()
        assert payment.status == Payment.Status.REFUNDED

        refund = Refund.objects.filter(payment_id=payment.pk).first()
        assert refund is not None
        assert refund.state == Refund.State.COMPLETED

        # BLOCKER fix: the refund must REVERSE the allocation + invoice status, so
        # the invoice is no longer PAID and the outstanding balance is restored.
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.ISSUED, "refunded invoice must leave PAID"
        from apps.finance.models import PaymentAllocation

        assert PaymentAllocation.objects.filter(payment_id=payment.pk).count() == 0
        assert finance_selectors.outstanding_balance(inv.student_id) == Decimal(AMOUNT_UZS)


def test_refund_explicit_zero_amount_is_rejected_not_full_refund(invoice_a):
    """R7 (money): an EXPLICIT amount of 0 must be rejected (positivity 400), NOT
    silently coalesced to a full refund. refund_payment presence-checks the amount, so
    Decimal('0') reaches request_refund's `amount <= 0` guard instead of `0 or full`."""
    from apps.finance.models import Invoice, Refund
    from apps.payments import services
    from apps.payments.models import Payment
    from core.exceptions import ValidationException

    tenant_a, inv = invoice_a
    with schema_context(tenant_a.schema_name):
        payment = services.process_click_complete(
            payload={"click_trans_id": "click-zero", "merchant_trans_id": inv.number, "amount": AMOUNT_UZS},
            invoice=inv,
        )
        with pytest.raises(ValidationException) as exc:
            services.refund_payment(payment_id=payment.pk, amount_uzs=Decimal("0"), reason="oops")
        assert exc.value.code == "invalid_amount"
        # Nothing was refunded: no Refund row, payment still COMPLETED, invoice still PAID.
        assert not Refund.objects.filter(payment_id=payment.pk).exists()
        payment.refresh_from_db()
        assert payment.status == Payment.Status.COMPLETED
        inv.refresh_from_db()
        assert inv.status == Invoice.Status.PAID


def test_refund_cannot_be_applied_twice(invoice_a):
    """A second refund on an already-fully-refunded payment is rejected — the
    payment is no longer COMPLETED, and the net-paid guard would also block it."""
    tenant_a, inv = invoice_a
    from apps.payments import services
    from core.exceptions import UnprocessableEntity

    with schema_context(tenant_a.schema_name):
        payment = services.process_click_complete(
            payload={
                "click_trans_id": "click-refund-twice",
                "merchant_trans_id": inv.number,
                "amount": AMOUNT_UZS,
            },
            invoice=inv,
        )
        services.refund_payment(payment_id=payment.pk, reason="first")
        with pytest.raises(UnprocessableEntity):
            services.refund_payment(payment_id=payment.pk, reason="second")


# --------------------------------------------------------------------------- #
# Reconciliation math (D3-B-10)
# --------------------------------------------------------------------------- #
def test_reconciliation_totals_and_mismatch(invoice_a):
    tenant_a, inv = invoice_a
    from django.utils import timezone

    from apps.payments import selectors, services
    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        # one fully-allocated payment (matches), one completed-but-unallocated (mismatch)
        matched = services.process_click_complete(
            payload={
                "click_trans_id": "rec-matched",
                "merchant_trans_id": inv.number,
                "amount": AMOUNT_UZS,
            },
            invoice=inv,
        )
        unallocated, _ = services.get_or_create_payment(
            idempotency_key="rec-unalloc",
            provider="cash",
            amount_uzs=Decimal("50000.00"),
            account_ref="INV-NONE",
        )
        Payment.objects.filter(pk=unallocated.pk).update(
            status=Payment.Status.COMPLETED, paid_at=timezone.now()
        )

        report = selectors.reconciliation(on=timezone.localdate())

        assert report["total_paid_uzs"] == "200000.00"  # 150000 + 50000
        assert report["total_allocated_uzs"] == "150000.00"  # only the matched one
        # the unallocated completed payment is a mismatch
        mismatch_ids = {m["payment_id"] for m in report["mismatches"]}
        assert unallocated.pk in mismatch_ids
        assert matched.pk not in mismatch_ids
        assert report["mismatch_count"] == 1
        assert report["by_provider"]["click"] == "150000.00"
        assert report["by_provider"]["cash"] == "50000.00"


# --------------------------------------------------------------------------- #
# Click/Uzum amount integrity (MAJOR): a provider amount != invoice total is
# rejected and never completes a Payment (Payme already guards via -31001).
# --------------------------------------------------------------------------- #
def test_click_complete_rejects_amount_mismatch(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from apps.payments.models import Payment
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        with pytest.raises(ValidationException) as exc:
            services.process_click_complete(
                payload={
                    "click_trans_id": "click-wrong-amt",
                    "merchant_trans_id": inv.number,
                    "amount": "1.00",  # far less than the 150000 invoice total
                },
                invoice=inv,
            )
        assert exc.value.code == "amount_mismatch"
        # nothing was completed
        assert not Payment.objects.filter(provider_txn_id="click-wrong-amt").exists()


def test_uzum_complete_rejects_amount_mismatch(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from apps.payments.models import Payment
    from core.exceptions import ValidationException

    with schema_context(tenant_a.schema_name):
        with pytest.raises(ValidationException) as exc:
            services.process_uzum_payment(
                payload={"transaction_id": "uzum-wrong-amt", "order_id": inv.number, "amount": "1.00"},
                invoice=inv,
            )
        assert exc.value.code == "amount_mismatch"
        assert not Payment.objects.filter(provider_txn_id="uzum-wrong-amt").exists()


# --------------------------------------------------------------------------- #
# Regression (HIGH, this-session bug hunt): the checkout merchant reference is
# echoed back on the completion callback, where the webhook resolves the invoice
# by Invoice.number. It MUST be invoice.number — sending the Payment PK made
# every real Click/Uzum callback resolve to number="<pk>" (no such invoice) so
# the payment was ACKed to the provider yet the invoice was never credited.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("provider", "ref_key"), [("click", "merchant_trans_id"), ("uzum", "order_id")]
)
def test_checkout_merchant_ref_is_invoice_number_not_pk(invoice_a, provider, ref_key):
    tenant_a, inv = invoice_a
    from apps.payments import services

    with schema_context(tenant_a.schema_name):
        payload = services.create_checkout(
            invoice_id=inv.id, provider=provider, idempotency_key=f"co-{provider}-1"
        )
        # The provider echoes this reference back on the Complete callback, where
        # the webhook does Invoice.objects.filter(number=<ref>). It must be the
        # invoice number so that lookup resolves — not the (unrelated) Payment PK.
        assert payload[ref_key] == inv.number
        assert payload[ref_key] != str(payload["payment_id"])


# --------------------------------------------------------------------------- #
# Regression (HIGH, this-session bug hunt): the checkout rounds the invoice total
# to whole soum (Click/Uzum are charged in soum), but the amount check compared
# the callback exactly against a fractional total_uzs — so a fractional invoice
# (e.g. from a percentage discount) was PERMANENTLY unpayable online.
# --------------------------------------------------------------------------- #
def test_click_complete_credits_fractional_invoice_at_rounded_soum(tenant_a):
    from apps.payments import services
    from apps.payments.models import Payment

    helpers.seed_provider_configs(tenant_a)
    inv = helpers.seed_open_invoice(tenant_a, number="INV-2026-000777", amount_uzs="149999.99")
    with schema_context(tenant_a.schema_name):
        # Checkout charges the whole-soum rounded figure (149999.99 -> 150000).
        payload = services.create_checkout(
            invoice_id=inv.id, provider="click", idempotency_key="co-frac-1"
        )
        assert "amount=150000" in payload["redirect_url"]
        # The provider reports the amount it charged; the webhook must accept it.
        payment = services.process_click_complete(
            payload={
                "click_trans_id": "click-frac",
                "merchant_trans_id": inv.number,
                "amount": "150000",
            },
            invoice=inv,
        )
        payment.refresh_from_db()
        assert payment.status == Payment.Status.COMPLETED


def test_click_complete_still_rejects_underpay_on_fractional_invoice(tenant_a):
    from apps.payments import services
    from core.exceptions import ValidationException

    helpers.seed_provider_configs(tenant_a)
    inv = helpers.seed_open_invoice(tenant_a, number="INV-2026-000778", amount_uzs="149999.99")
    with schema_context(tenant_a.schema_name):
        # 149999 (truncated, one soum short) must NOT satisfy the 150000 charge.
        with pytest.raises(ValidationException) as exc:
            services.process_click_complete(
                payload={
                    "click_trans_id": "click-frac-under",
                    "merchant_trans_id": inv.number,
                    "amount": "149999",
                },
                invoice=inv,
            )
        assert exc.value.code == "amount_mismatch"


# --------------------------------------------------------------------------- #
# Over-allocation during auto-allocate must NOT roll back the completion
# (BLOCKER): completing a payment whose invoice is already PAID still produces a
# COMPLETED Payment with allocation_status=MANUAL_REVIEW — no exception.
# --------------------------------------------------------------------------- #
def test_auto_allocate_failure_completes_payment_manual_review(invoice_a, django_capture_on_commit_callbacks):
    tenant_a, inv = invoice_a
    from apps.finance.models import Invoice
    from apps.payments import services
    from apps.payments.models import Payment
    from apps.payments.signals import payment_completed

    fired: list[dict] = []
    payment_completed.connect(
        lambda sender, **kw: fired.append(kw), dispatch_uid="test.manualreview.completed", weak=False
    )
    try:
        with schema_context(tenant_a.schema_name):
            # First payment fully pays the invoice (auto-allocated).
            with django_capture_on_commit_callbacks(execute=True):
                first = services.process_click_complete(
                    payload={
                        "click_trans_id": "click-pays-it",
                        "merchant_trans_id": inv.number,
                        "amount": AMOUNT_UZS,
                    },
                    invoice=inv,
                )
            assert first.allocation_status == Payment.Allocation.ALLOCATED
            inv.refresh_from_db()
            assert inv.status == Invoice.Status.PAID

            # A second (duplicate/late) charge against the now-PAID invoice: the
            # allocation can't match an open invoice, but the payment is real
            # money — it must still complete and be flagged for manual review.
            second, _ = services.get_or_create_payment(
                idempotency_key="dup-charge",
                provider="click",
                amount_uzs=Decimal(AMOUNT_UZS),
                account_ref=inv.number,
                metadata={"invoice_id": inv.id, "student_id": inv.student_id},
            )
            with django_capture_on_commit_callbacks(execute=True):
                services.mark_payment_completed(payment_id=second.pk, provider_txn_id="dup-1")
            second.refresh_from_db()
            assert second.status == Payment.Status.COMPLETED, "completion must not roll back"
            assert second.allocation_status == Payment.Allocation.MANUAL_REVIEW
    finally:
        payment_completed.disconnect(dispatch_uid="test.manualreview.completed")

    # both completions emitted their signal (the second did NOT 500)
    assert len(fired) == 2


# --------------------------------------------------------------------------- #
# Cash intake stamps the cashier shift so reconciliation reflects real cash
# (MAJOR): a cash payment in an open shift appears in the shift report + the
# discrepancy math.
# --------------------------------------------------------------------------- #
def test_cash_payment_stamps_shift_and_shows_in_report(invoice_a, user_in):
    tenant_a, inv = invoice_a
    from apps.finance import selectors as finance_selectors
    from apps.finance import services as finance_services
    from apps.payments import services
    from apps.payments.models import Payment
    from core.permissions import Role

    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        from apps.org.tests.factories import BranchFactory

        branch = BranchFactory()
        shift = finance_services.open_cashier_shift(
            cashier=cashier, branch=branch, opening_cash_uzs=Decimal("10000.00")
        )

        payment = services.create_cash_payment(invoice_id=inv.id, cashier=cashier)
        assert payment.provider == Payment.Method.CASH
        assert payment.status == Payment.Status.COMPLETED
        assert payment.cashier_shift_id == shift.pk

        # the report now reflects the cash taken in
        report = finance_selectors.cashier_shift_report(shift=shift)
        assert report["totals_by_provider"].get("cash") == AMOUNT_UZS
        assert report["payments_total_uzs"] == AMOUNT_UZS

        # discrepancy = closing - (opening + cash_in); cash_in is now non-zero
        closed = finance_services.close_cashier_shift(shift=shift, closing_cash_uzs=Decimal("160000.00"))
        # 160000 - (10000 + 150000) == 0
        assert closed.discrepancy_uzs == Decimal("0.00")


def test_cash_payment_requires_open_shift(invoice_a, user_in):
    tenant_a, inv = invoice_a
    from apps.payments import services
    from core.exceptions import UnprocessableEntity
    from core.permissions import Role

    cashier = user_in(tenant_a, roles=[Role.CASHIER])
    with schema_context(tenant_a.schema_name):
        with pytest.raises(UnprocessableEntity) as exc:
            services.create_cash_payment(invoice_id=inv.id, cashier=cashier)
        assert exc.value.code == "no_open_shift"


# --------------------------------------------------------------------------- #
# ProviderConfig credentials write-only (D3-B-1 / TD-11)
# --------------------------------------------------------------------------- #
def test_provider_config_serializer_hides_credentials(invoice_a):
    tenant_a, _ = invoice_a
    from apps.payments.models import ProviderConfig
    from apps.payments.presenters import provider_config_to_dict

    with schema_context(tenant_a.schema_name):
        config = ProviderConfig.objects.get(provider="payme")
        data = provider_config_to_dict(config)
        assert "payme_key" not in data
        assert "payme_test_key" not in data
        assert "uzum_api_key" not in data
        assert "click_secret_key" not in data
        # non-secret fields still present
        assert data["provider"] == "payme"


def test_provider_config_patch_rejects_null_credential_and_is_active(invoice_a, user_in, as_user):
    """An explicit JSON null on a credential/is_active must be a 400 — never a silent
    wipe of a stored secret (which would break signature checks) or a silent deactivate.
    And a long key (>128, the old view cap) up to the model's 255 is accepted."""
    from apps.payments.models import ProviderConfig
    from core.permissions import Role

    tenant_a, _ = invoice_a
    director = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        config = ProviderConfig.objects.get(provider="payme")
        cfg_id = config.id

    client = as_user(tenant_a, director)
    base = f"/api/v1/payments/provider-configs/{cfg_id}/"
    assert client.patch(base, {"payme_key": None}, format="json").status_code == 400
    assert client.patch(base, {"is_active": None}, format="json").status_code == 400
    long_key = "k" * 200  # > the old 128 view cap, <= the model's 255
    assert client.patch(base, {"payme_key": long_key}, format="json").status_code == 200
    with schema_context(tenant_a.schema_name):
        config.refresh_from_db()
        assert config.payme_key == long_key  # the valid long key persisted
        assert config.is_active is True  # never deactivated by the rejected null


# --------------------------------------------------------------------------- #
# Manual allocation across multiple lines (regression: dropped-line money bug)
# --------------------------------------------------------------------------- #
def test_allocate_manual_applies_every_line(tenant_a):
    """allocate_manual used to loop finance.allocate_payment (idempotent per
    payment_id): only the FIRST line landed while the payment was marked ALLOCATED,
    silently dropping the rest. Every (invoice, amount) line must now be applied."""
    from apps.finance.models import Invoice, PaymentAllocation
    from apps.payments import services
    from apps.payments.models import Payment

    inv_a = helpers.seed_open_invoice(tenant_a, number="INV-2026-000010", amount_uzs="100000.00")
    inv_b = helpers.seed_open_invoice(tenant_a, number="INV-2026-000011", amount_uzs="60000.00")
    with schema_context(tenant_a.schema_name):
        payment, _ = services.get_or_create_payment(
            idempotency_key="alloc-multi-1",
            provider="payme",
            amount_uzs=Decimal("160000.00"),
            account_ref=inv_a.number,
            metadata={},
        )
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.COMPLETED)

        result = services.allocate_manual(
            payment_id=payment.pk,
            allocations=[
                {"invoice": inv_a.pk, "amount": Decimal("100000.00")},
                {"invoice": inv_b.pk, "amount": Decimal("60000.00")},
            ],
        )
        assert result.allocation_status == Payment.Allocation.ALLOCATED
        allocs = PaymentAllocation.objects.filter(payment_id=payment.pk)
        assert allocs.filter(invoice=inv_a).count() == 1
        assert allocs.filter(invoice=inv_b).count() == 1  # was dropped by the bug
        assert sum(a.amount_uzs for a in allocs) == Decimal("160000.00")
        inv_a.refresh_from_db()
        inv_b.refresh_from_db()
        assert inv_a.status == Invoice.Status.PAID
        assert inv_b.status == Invoice.Status.PAID


def test_allocate_manual_rejects_over_payment_amount(tenant_a):
    """The total of the lines may not exceed the real money received."""
    from apps.payments import services
    from apps.payments.models import Payment
    from core.exceptions import StarforgeError

    inv = helpers.seed_open_invoice(tenant_a, number="INV-2026-000012", amount_uzs="100000.00")
    with schema_context(tenant_a.schema_name):
        payment, _ = services.get_or_create_payment(
            idempotency_key="alloc-over-1",
            provider="payme",
            amount_uzs=Decimal("50000.00"),
            account_ref=inv.number,
            metadata={},
        )
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.COMPLETED)
        with pytest.raises(StarforgeError):
            services.allocate_manual(
                payment_id=payment.pk,
                allocations=[{"invoice": inv.pk, "amount": Decimal("90000.00")}],
            )
