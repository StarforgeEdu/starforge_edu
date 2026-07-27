"""Finance-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import factory

from apps.finance.models import (
    CashierShift,
    Discount,
    FeeSchedule,
    Invoice,
    InvoiceLine,
)
from apps.org.tests.factories import BranchFactory
from apps.students.tests.factories import StudentProfileFactory
from apps.users.tests.factories import UserFactory


class FeeScheduleFactory(factory.django.DjangoModelFactory[FeeSchedule]):
    class Meta:
        model = FeeSchedule

    name = factory.Sequence(lambda n: f"Tuition {n}")
    cohort = None  # center-wide default by default
    amount_uzs = Decimal("1000000.00")
    billing_period = FeeSchedule.BillingPeriod.MONTHLY
    due_day_of_month = 5
    is_active = True


class InvoiceFactory(factory.django.DjangoModelFactory[Invoice]):
    class Meta:
        model = Invoice

    number = factory.Sequence(lambda n: f"INV-2026-{n + 1:06d}")
    student = factory.SubFactory(StudentProfileFactory)
    status = Invoice.Status.ISSUED
    issue_date = date(2026, 6, 1)
    due_date = date(2026, 6, 5)
    currency = "UZS"
    total_uzs = Decimal("1000000.00")


class InvoiceLineFactory(factory.django.DjangoModelFactory[InvoiceLine]):
    class Meta:
        model = InvoiceLine

    invoice = factory.SubFactory(InvoiceFactory)
    description = "Tuition"
    line_type = InvoiceLine.LineType.TUITION
    quantity = Decimal("1")
    unit_price_uzs = Decimal("1000000.00")
    amount_uzs = Decimal("1000000.00")


class DiscountFactory(factory.django.DjangoModelFactory[Discount]):
    class Meta:
        model = Discount

    student = factory.SubFactory(StudentProfileFactory)
    discount_type = Discount.DiscountType.SCHOLARSHIP
    percent = Decimal("10.00")
    fixed_amount_uzs = None
    is_active = True


class CashierShiftFactory(factory.django.DjangoModelFactory[CashierShift]):
    class Meta:
        model = CashierShift

    cashier = factory.SubFactory(UserFactory)
    branch = factory.SubFactory(BranchFactory)
    status = CashierShift.Status.OPEN
    opening_cash_uzs = Decimal("0.00")
