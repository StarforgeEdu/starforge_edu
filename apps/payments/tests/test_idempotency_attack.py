"""D3-F-4 — payment idempotency-key reuse.

``Payment.idempotency_key`` is unique (D3-B-6). Two ``create_checkout`` calls
that carry the same key must collapse to exactly ONE Payment row — the second
returns the existing payment's pk, never inserts a duplicate.

Eager Celery (test settings) means there is no true concurrency to race, so we
exercise the sequential-replay contract (the second call returns the same pk)
AND drive the same key concurrently-shaped via two back-to-back service calls,
asserting the unique constraint holds. Lane code (apps.payments.services /
views) is imported lazily; the orchestrator runs this on Postgres after merge.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.payments.tests import _helpers as helpers

pytestmark = pytest.mark.django_db

AMOUNT_UZS = "150000.00"
IDEMP_KEY = "client-idem-key-abc123"


@pytest.fixture
def invoice_a(tenant_a):
    helpers.seed_provider_configs(tenant_a)
    inv = helpers.seed_open_invoice(tenant_a, number="INV-2026-000001", amount_uzs=AMOUNT_UZS)
    return tenant_a, inv


# --------------------------------------------------------------------------- #
# Service-level: same idempotency_key twice -> one Payment, same pk
# --------------------------------------------------------------------------- #
def test_create_checkout_same_key_returns_existing_payment(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services

    with schema_context(tenant_a.schema_name):
        first = services.create_checkout(invoice_id=inv.id, provider="payme", idempotency_key=IDEMP_KEY)
        second = services.create_checkout(invoice_id=inv.id, provider="payme", idempotency_key=IDEMP_KEY)
        first_id = _payment_id(first)
        second_id = _payment_id(second)
        assert first_id == second_id

        from apps.payments.models import Payment

        assert Payment.objects.filter(idempotency_key=IDEMP_KEY).count() == 1


def test_create_checkout_different_keys_two_payments(invoice_a):
    tenant_a, inv = invoice_a
    from apps.payments import services

    with schema_context(tenant_a.schema_name):
        a = services.create_checkout(invoice_id=inv.id, provider="payme", idempotency_key="key-1")
        b = services.create_checkout(invoice_id=inv.id, provider="payme", idempotency_key="key-2")
        assert _payment_id(a) != _payment_id(b)

        from apps.payments.models import Payment

        assert Payment.objects.filter(idempotency_key__in=["key-1", "key-2"]).count() == 2


def test_payment_idempotency_key_unique_constraint(invoice_a):
    """The unique constraint is the load-bearing guard — a raw duplicate insert
    must IntegrityError, proving idempotency isn't just app-level."""
    tenant_a, inv = invoice_a
    from django.db import IntegrityError, transaction

    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        Payment.objects.create(
            provider="payme",
            amount_uzs=AMOUNT_UZS,
            status="pending",
            idempotency_key="dupe-key",
            account_ref=inv.number,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                provider="payme",
                amount_uzs=AMOUNT_UZS,
                status="pending",
                idempotency_key="dupe-key",
                account_ref=inv.number,
            )


# --------------------------------------------------------------------------- #
# View-level: the Idempotency-Key header reaches the service (CODE-GUIDE §8)
# --------------------------------------------------------------------------- #
def test_checkout_endpoint_idempotency_header_one_payment(invoice_a, user_in, as_user):
    tenant_a, inv = invoice_a
    cashier = user_in(tenant_a, roles=["accountant"])
    client = as_user(tenant_a, cashier)
    # CheckoutSerializer field is `invoice` (an int), not `invoice_id`.
    body = {"invoice": inv.id, "provider": "payme"}
    headers = {"HTTP_IDEMPOTENCY_KEY": IDEMP_KEY}

    r1 = client.post("/api/v1/payments/checkout/", body, format="json", **headers)
    r2 = client.post("/api/v1/payments/checkout/", body, format="json", **headers)
    assert r1.status_code in (200, 201), r1.content
    assert r2.status_code in (200, 201), r2.content
    assert r1.json()["data"]["payment_id"] == r2.json()["data"]["payment_id"]

    from apps.payments.models import Payment

    with schema_context(tenant_a.schema_name):
        assert Payment.objects.filter(idempotency_key=IDEMP_KEY).count() == 1


def test_two_cash_payments_same_invoice_are_both_recorded(invoice_a, user_in, as_user):
    """R4/CONF6 (money): without an Idempotency-Key header, each cash POST is a DISTINCT
    payment. A cashier legitimately takes several payments toward one invoice in a shift;
    the old derived per-(invoice, shift) key swallowed every payment after the first
    (silent cash loss). Two headerless POSTs must create TWO completed payments."""
    from apps.finance import services as finance_services
    from apps.org.tests.factories import BranchFactory
    from apps.payments.models import Payment

    tenant_a, inv = invoice_a
    cashier = user_in(tenant_a, roles=["cashier"])
    with schema_context(tenant_a.schema_name):
        finance_services.open_cashier_shift(
            cashier=cashier, branch=BranchFactory(), opening_cash_uzs=Decimal("0.00")
        )
    client = as_user(tenant_a, cashier)
    r1 = client.post("/api/v1/payments/cash/", {"invoice": inv.id, "amount_uzs": "50000.00"}, format="json")
    r2 = client.post("/api/v1/payments/cash/", {"invoice": inv.id, "amount_uzs": "30000.00"}, format="json")
    assert r1.status_code in (200, 201), r1.content
    assert r2.status_code in (200, 201), r2.content
    assert r1.json()["data"]["id"] != r2.json()["data"]["id"]  # two distinct payments, not coalesced
    with schema_context(tenant_a.schema_name):
        assert Payment.objects.filter(provider=Payment.Method.CASH, status="completed").count() == 2


def test_cash_double_submit_same_amount_is_coalesced(invoice_a, user_in, as_user):
    """R5/PLAUS3: a headerless double-submit of the SAME cash amount (double-click /
    network retry) must NOT double-credit the drawer. The amount-derived fallback key
    coalesces it to ONE payment; distinct amounts (covered elsewhere) still split."""
    from apps.finance import services as finance_services
    from apps.org.tests.factories import BranchFactory
    from apps.payments.models import Payment

    tenant_a, inv = invoice_a
    cashier = user_in(tenant_a, roles=["cashier"])
    with schema_context(tenant_a.schema_name):
        finance_services.open_cashier_shift(
            cashier=cashier, branch=BranchFactory(), opening_cash_uzs=Decimal("0.00")
        )
    client = as_user(tenant_a, cashier)
    body = {"invoice": inv.id, "amount_uzs": "50000.00"}
    r1 = client.post("/api/v1/payments/cash/", body, format="json")  # no Idempotency-Key
    r2 = client.post("/api/v1/payments/cash/", body, format="json")  # accidental resubmit
    assert r1.status_code in (200, 201), r1.content
    assert r2.status_code in (200, 201), r2.content
    assert r1.json()["data"]["id"] == r2.json()["data"]["id"]  # coalesced, one payment
    with schema_context(tenant_a.schema_name):
        assert Payment.objects.filter(provider=Payment.Method.CASH, status="completed").count() == 1


def test_cash_equal_amount_installments_in_different_windows_both_record(invoice_a, user_in, as_user):
    """R6 (money): two legitimate EQUAL-amount cash installments toward one invoice, taken
    in different idempotency windows (minutes apart), must BOTH record — the headerless
    time-windowed key only coalesces a same-window resubmit, not genuine later repeats."""
    import time_machine

    from apps.finance import services as finance_services
    from apps.org.tests.factories import BranchFactory
    from apps.payments.models import Payment

    tenant_a, inv = invoice_a
    cashier = user_in(tenant_a, roles=["cashier"])
    client = as_user(tenant_a, cashier)
    body = {"invoice": inv.id, "amount_uzs": "50000.00"}
    with time_machine.travel("2026-06-16 10:00:00 +05:00", tick=False):
        with schema_context(tenant_a.schema_name):
            finance_services.open_cashier_shift(
                cashier=cashier, branch=BranchFactory(), opening_cash_uzs=Decimal("0.00")
            )
        r1 = client.post("/api/v1/payments/cash/", body, format="json")
    with time_machine.travel("2026-06-16 10:05:00 +05:00", tick=False):  # 5 min later, new window
        r2 = client.post("/api/v1/payments/cash/", body, format="json")
    assert r1.status_code in (200, 201), r1.content
    assert r2.status_code in (200, 201), r2.content
    assert r1.json()["data"]["id"] != r2.json()["data"]["id"]  # both recorded, not coalesced
    with schema_context(tenant_a.schema_name):
        assert Payment.objects.filter(provider=Payment.Method.CASH, status="completed").count() == 2


def _payment_id(result) -> int:
    """create_checkout may return a Payment, a dict, or an id — normalize."""
    if isinstance(result, dict):
        return int(result.get("payment_id") or result["id"])
    return int(getattr(result, "id", result))
