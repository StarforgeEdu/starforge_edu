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
    IStatementExportRepository,
)
from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    PaymentMethod,
    StatementExport,
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

    def scoped(
        self,
        *,
        user,
        roles: set[str],
        permission: str = "finance:read",
    ) -> QuerySet[Invoice]:
        return selectors.scoped_invoice_summaries(
            user=user,
            roles=roles,
            permission=permission,
        )

    def get_scoped(
        self,
        *,
        pk: int,
        user,
        roles: set[str],
        permission: str = "finance:read",
    ) -> Invoice | None:
        return (
            selectors.scoped_invoices(
                user=user,
                roles=roles,
                permission=permission,
            )
            .filter(pk=pk)
            .first()
        )


class StatementExportRepository(
    BaseRepository[StatementExport],
    IStatementExportRepository,
):
    model = StatementExport

    def query(self) -> QuerySet[StatementExport]:
        return StatementExport.objects.select_related(
            "student__user",
            "requested_by",
        ).prefetch_related("invoice_links")

    def get(self, pk) -> StatementExport | None:
        return self.query().filter(pk=pk).first()


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
            "branch",
            "payment_method",
            "created_by",
            "approved_by",
            "paid_by",
            "approval_request",
        ).all()

    def get(self, pk: int) -> Expense | None:
        return self.query().filter(pk=pk).first()


class CashierShiftRepository(BaseRepository[CashierShift], ICashierShiftRepository):
    model = CashierShift

    def query(self) -> QuerySet[CashierShift]:
        return CashierShift.objects.select_related("cashier", "branch", "closed_by").all()
