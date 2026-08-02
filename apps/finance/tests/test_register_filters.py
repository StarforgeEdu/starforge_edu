"""Branch/date filtering contracts for the CEO-facing finance registers."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.finance.models import Expense, Refund
from apps.finance.tests.factories import InvoiceFactory
from apps.org.tests.factories import BranchFactory
from apps.payments.models import Payment
from apps.payments.tests.factories import PaymentFactory
from apps.students.tests.factories import StudentProfileFactory
from core.permissions import Role

pytestmark = pytest.mark.django_db

INVOICES_URL = "/api/v1/finance/invoices/"
PAYMENTS_URL = "/api/v1/payments/"
EXPENSES_URL = "/api/v1/finance/expenses/"
REFUNDS_URL = "/api/v1/finance/refunds/"
REGISTER_URLS = (INVOICES_URL, PAYMENTS_URL, EXPENSES_URL, REFUNDS_URL)


def _at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return timezone.make_aware(
        datetime(year, month, day, hour),
        timezone.get_current_timezone(),
    )


def _ids(response) -> set[int]:
    assert response.status_code == 200, response.content
    return {row["id"] for row in response.json()["data"]}


def test_invoice_expense_and_refund_filters_use_branch_and_inclusive_business_dates(
    tenant_a, user_in, as_user
):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="register-a")
        branch_b = BranchFactory(slug="register-b")
        student_a = StudentProfileFactory(branch=branch_a)
        student_b = StudentProfileFactory(branch=branch_b)

        invoice_a_start = InvoiceFactory(
            number="INV-REGISTER-A-START",
            student=student_a,
            issue_date=date(2026, 7, 1),
        )
        invoice_a_end = InvoiceFactory(
            number="INV-REGISTER-A-END",
            student=student_a,
            issue_date=date(2026, 7, 31),
        )
        InvoiceFactory(
            number="INV-REGISTER-A-OUTSIDE",
            student=student_a,
            issue_date=date(2026, 6, 30),
        )
        invoice_b = InvoiceFactory(
            number="INV-REGISTER-B",
            student=student_b,
            issue_date=date(2026, 7, 15),
        )

        expense_a = Expense.objects.create(
            branch=branch_a,
            description="Branch A July expense",
            amount_uzs=Decimal("100.00"),
        )
        expense_a_outside = Expense.objects.create(
            branch=branch_a,
            description="Branch A June expense",
            amount_uzs=Decimal("100.00"),
        )
        expense_b = Expense.objects.create(
            branch=branch_b,
            description="Branch B July expense",
            amount_uzs=Decimal("100.00"),
        )
        Expense.objects.filter(pk=expense_a.pk).update(created_at=_at(2026, 7, 31, 23))
        Expense.objects.filter(pk=expense_a_outside.pk).update(created_at=_at(2026, 6, 30))
        Expense.objects.filter(pk=expense_b.pk).update(created_at=_at(2026, 7, 15))

        refund_a = Refund.objects.create(
            invoice=invoice_a_start,
            amount_uzs=Decimal("25.00"),
        )
        refund_a_outside = Refund.objects.create(
            invoice=invoice_a_start,
            amount_uzs=Decimal("25.00"),
        )
        refund_b = Refund.objects.create(
            invoice=invoice_b,
            amount_uzs=Decimal("25.00"),
        )
        Refund.objects.filter(pk=refund_a.pk).update(created_at=_at(2026, 7, 1, 0))
        Refund.objects.filter(pk=refund_a_outside.pk).update(created_at=_at(2026, 8, 1))
        Refund.objects.filter(pk=refund_b.pk).update(created_at=_at(2026, 7, 15))

    director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch_a)
    client = as_user(tenant_a, director)
    filters = {
        "branch": branch_a.pk,
        "date_from": "2026-07-01",
        "date_to": "2026-07-31",
    }

    assert _ids(client.get(INVOICES_URL, filters)) == {
        invoice_a_start.pk,
        invoice_a_end.pk,
    }
    assert _ids(client.get(EXPENSES_URL, filters)) == {expense_a.pk}
    assert _ids(client.get(REFUNDS_URL, filters)) == {refund_a.pk}


def test_payment_filter_uses_paid_at_with_created_at_fallback(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="payment-register-a")
        branch_b = BranchFactory(slug="payment-register-b")
        invoice_a = InvoiceFactory(
            number="INV-PAYMENT-REGISTER-A",
            student=StudentProfileFactory(branch=branch_a),
        )
        invoice_b = InvoiceFactory(
            number="INV-PAYMENT-REGISTER-B",
            student=StudentProfileFactory(branch=branch_b),
        )
        paid_in_range = PaymentFactory(
            account_ref=invoice_a.number,
            branch_at_payment=branch_a,
            status=Payment.Status.COMPLETED,
            paid_at=_at(2026, 7, 15),
        )
        unpaid_in_range = PaymentFactory(
            account_ref=invoice_a.number,
            branch_at_payment=branch_a,
            status=Payment.Status.PENDING,
            paid_at=None,
        )
        paid_outside = PaymentFactory(
            account_ref=invoice_a.number,
            branch_at_payment=branch_a,
            status=Payment.Status.COMPLETED,
            paid_at=_at(2026, 8, 1),
        )
        other_branch = PaymentFactory(
            account_ref=invoice_b.number,
            branch_at_payment=branch_b,
            status=Payment.Status.COMPLETED,
            paid_at=_at(2026, 7, 15),
        )
        # Prove paid_at wins when present, while a row without paid_at falls back
        # to created_at. Both rows were initially created "now" by auto_now_add.
        Payment.objects.filter(pk=unpaid_in_range.pk).update(created_at=_at(2026, 7, 20))
        Payment.objects.filter(pk=paid_outside.pk).update(created_at=_at(2026, 7, 20))

    director = user_in(tenant_a, roles=[Role.DIRECTOR], branch=branch_a)
    client = as_user(tenant_a, director)
    response = client.get(
        PAYMENTS_URL,
        {
            "branch": branch_a.pk,
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
        },
    )
    assert _ids(response) == {paid_in_range.pk, unpaid_in_range.pk}
    assert paid_outside.pk not in _ids(response)
    assert other_branch.pk not in _ids(response)


@pytest.mark.parametrize("url", REGISTER_URLS)
@pytest.mark.parametrize(
    ("filters", "field"),
    [
        ({"branch": "not-an-integer"}, "branch"),
        ({"branch": "0"}, "branch"),
        ({"date_from": "2026-02-30"}, "date_from"),
        ({"date_from": "2026-08-01", "date_to": "2026-07-31"}, "date_to"),
    ],
)
def test_register_filters_reject_malformed_and_reversed_values(
    tenant_a, user_in, as_user, url, filters, field
):
    director = user_in(tenant_a, roles=[Role.DIRECTOR])
    response = as_user(tenant_a, director).get(url, filters)

    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"
    assert set(response.json()["errors"]) == {field}


def test_explicit_branch_filter_cannot_widen_membership_scope(tenant_a, user_in, as_user):
    with schema_context(tenant_a.schema_name):
        branch_a = BranchFactory(slug="scoped-register-a")
        branch_b = BranchFactory(slug="scoped-register-b")
        invoice_b = InvoiceFactory(
            number="INV-SCOPED-REGISTER-B",
            student=StudentProfileFactory(branch=branch_b),
            issue_date=date(2026, 7, 15),
        )
        Expense.objects.create(
            branch=branch_b,
            description="Out-of-scope expense",
            amount_uzs=Decimal("100.00"),
        )
        Refund.objects.create(
            invoice=invoice_b,
            amount_uzs=Decimal("25.00"),
        )
        PaymentFactory(
            account_ref=invoice_b.number,
            branch_at_payment=branch_b,
            status=Payment.Status.COMPLETED,
            paid_at=_at(2026, 7, 15),
        )

    accountant = user_in(tenant_a, roles=[Role.ACCOUNTANT], branch=branch_a)
    client = as_user(tenant_a, accountant)
    for url in REGISTER_URLS:
        response = client.get(url, {"branch": branch_b.pk})
        assert response.status_code == 200, response.content
        assert response.json()["pagination"]["total"] == 0
        assert response.json()["data"] == []
