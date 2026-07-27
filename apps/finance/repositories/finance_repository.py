"""Finance repositories — the ORM touchpoints. Invoices read through the
preserved selectors.scoped_invoices (nuanced role scoping)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.finance import selectors
from apps.finance.interfaces.repositories import (
    ICashierShiftRepository,
    IDiscountRepository,
    IExpenseRepository,
    IFeeScheduleRepository,
    IInvoiceRepository,
    IPaymentMethodRepository,
)
from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    PaymentMethod,
)
from core.repositories import BaseRepository


class FeeScheduleRepository(BaseRepository[FeeSchedule], IFeeScheduleRepository):
    model = FeeSchedule

    def query(self) -> QuerySet[FeeSchedule]:
        return FeeSchedule.objects.select_related("cohort").all()

    def get(self, pk: int) -> FeeSchedule | None:
        return self.query().filter(pk=pk).first()


class InvoiceRepository(BaseRepository[Invoice], IInvoiceRepository):
    model = Invoice

    def scoped(self, *, user, roles: set[str]) -> QuerySet[Invoice]:
        return selectors.scoped_invoices(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user, roles: set[str]) -> Invoice | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()

    def get_by_pk(self, pk: int) -> Invoice | None:
        return selectors._invoice_base().filter(pk=pk).first()


class DiscountRepository(BaseRepository[Discount], IDiscountRepository):
    model = Discount

    def query(self) -> QuerySet[Discount]:
        return Discount.objects.select_related("student__user", "approved_by").all()

    def get(self, pk: int) -> Discount | None:
        return self.query().filter(pk=pk).first()


class PaymentMethodRepository(BaseRepository[PaymentMethod], IPaymentMethodRepository):
    model = PaymentMethod

    def query(self) -> QuerySet[PaymentMethod]:
        return PaymentMethod.objects.all()

    def get(self, pk: int) -> PaymentMethod | None:
        return PaymentMethod.objects.filter(pk=pk).first()


class ExpenseRepository(BaseRepository[Expense], IExpenseRepository):
    model = Expense

    def query(self) -> QuerySet[Expense]:
        return Expense.objects.select_related(
            "branch", "payment_method", "created_by", "approved_by", "paid_by"
        ).all()

    def get(self, pk: int) -> Expense | None:
        return self.query().filter(pk=pk).first()


class CashierShiftRepository(BaseRepository[CashierShift], ICashierShiftRepository):
    model = CashierShift

    def query(self) -> QuerySet[CashierShift]:
        return CashierShift.objects.select_related("cashier", "branch").all()

    def get(self, pk: int) -> CashierShift | None:
        return self.query().filter(pk=pk).first()
