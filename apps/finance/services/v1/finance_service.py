"""Finance service — thin orchestration over the preserved finance domain
functions (invoicing/FX/discounts numbering, the expense lifecycle, cashier
shifts, refunds) + the read repositories/selectors. The heavy money logic stays
VERBATIM in ``apps.finance.services`` (the package __init__), imported by
approvals/payments/students/selectors + the celery finance tasks. Fee schedules
and payment methods are plain CRUD (no domain fn), so the service does the ORM
create/update for those (with FKs already resolved by the view).
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.finance import selectors
from apps.finance import services as domain
from apps.finance.interfaces.repositories import (
    ICashierShiftRepository,
    IDiscountRepository,
    IExpenseRepository,
    IFeeScheduleRepository,
    IInvoiceRepository,
    IPaymentMethodRepository,
)
from apps.finance.interfaces.services import IFinanceService
from apps.finance.models import (
    CashierShift,
    Discount,
    Expense,
    FeeSchedule,
    Invoice,
    PaymentMethod,
    PaymentPlan,
)


class FinanceService(IFinanceService):
    def __init__(
        self,
        fee_schedule_repository: IFeeScheduleRepository,
        invoice_repository: IInvoiceRepository,
        discount_repository: IDiscountRepository,
        payment_method_repository: IPaymentMethodRepository,
        expense_repository: IExpenseRepository,
        cashier_shift_repository: ICashierShiftRepository,
    ) -> None:
        self._fee = fee_schedule_repository
        self._inv = invoice_repository
        self._disc = discount_repository
        self._pm = payment_method_repository
        self._exp = expense_repository
        self._shift = cashier_shift_repository

    # --- fee schedules ---
    def fee_schedules(self) -> QuerySet[FeeSchedule]:
        return self._fee.query()

    def fee_schedule(self, pk: int) -> FeeSchedule | None:
        return self._fee.get(pk)

    def create_fee_schedule(self, *, data: dict[str, Any]) -> FeeSchedule:
        return FeeSchedule.objects.create(**data)

    def update_fee_schedule(self, *, fee_schedule: FeeSchedule, changes: dict[str, Any]) -> FeeSchedule:
        for field, value in changes.items():
            setattr(fee_schedule, field, value)
        if changes:
            fee_schedule.save(update_fields=list(changes.keys()))
        return fee_schedule

    def delete_fee_schedule(self, *, fee_schedule: FeeSchedule) -> None:
        fee_schedule.delete()

    # --- invoices ---
    def invoices(self, *, user, roles: set[str]) -> QuerySet[Invoice]:
        return self._inv.scoped(user=user, roles=roles)

    def invoice(self, *, pk: int, user, roles: set[str]) -> Invoice | None:
        return self._inv.get_scoped(pk=pk, user=user, roles=roles)

    def issue_invoice(self, *, student_id: int, fee_schedule_id, lines, period: str, created_by) -> Invoice:
        return domain.issue_invoice(
            student_id=student_id,
            fee_schedule_id=fee_schedule_id,
            lines=lines,
            period=period,
            created_by=created_by,
        )

    def void_invoice(self, *, invoice: Invoice, actor) -> Invoice:
        return domain.void_invoice(invoice=invoice, actor=actor)

    def reload_invoice(self, *, pk: int, user, roles: set[str]) -> Invoice | None:
        return self._inv.get_scoped(pk=pk, user=user, roles=roles)

    def create_payment_plan(self, *, invoice: Invoice, installments: list[dict], created_by) -> PaymentPlan:
        return domain.create_payment_plan(invoice=invoice, installments=installments, created_by=created_by)

    # --- discounts ---
    def discounts(self) -> QuerySet[Discount]:
        return self._disc.query()

    def discount(self, pk: int) -> Discount | None:
        return self._disc.get(pk)

    def deactivate_discount(self, *, discount: Discount) -> Discount:
        if discount.is_active:
            discount.is_active = False
            discount.save(update_fields=["is_active", "updated_at"])
        return discount

    # --- payment methods ---
    def payment_methods(self) -> QuerySet[PaymentMethod]:
        return self._pm.query()

    def payment_method(self, pk: int) -> PaymentMethod | None:
        return self._pm.get(pk)

    def create_payment_method(self, *, data: dict[str, Any]) -> PaymentMethod:
        return PaymentMethod.objects.create(**data)

    def update_payment_method(
        self, *, payment_method: PaymentMethod, changes: dict[str, Any]
    ) -> PaymentMethod:
        for field, value in changes.items():
            setattr(payment_method, field, value)
        if changes:
            payment_method.save(update_fields=list(changes.keys()))
        return payment_method

    def delete_payment_method(self, *, payment_method: PaymentMethod) -> None:
        payment_method.delete()

    # --- expenses ---
    def expenses(self) -> QuerySet[Expense]:
        return self._exp.query()

    def expense(self, pk: int) -> Expense | None:
        return self._exp.get(pk)

    def create_expense(self, *, branch, description: str, amount_uzs, category: str, created_by) -> Expense:
        return domain.create_expense(
            branch=branch,
            description=description,
            amount_uzs=amount_uzs,
            category=category,
            created_by=created_by,
        )

    def approve_expense(self, *, expense_id: int, actor) -> Expense:
        return domain.approve_expense(expense_id=expense_id, actor=actor)

    def reject_expense(self, *, expense_id: int, reason: str, actor) -> Expense:
        return domain.reject_expense(expense_id=expense_id, reason=reason, actor=actor)

    def pay_expense(self, *, expense_id: int, payment_method_id: int, actor) -> Expense:
        return domain.pay_expense(expense_id=expense_id, payment_method_id=payment_method_id, actor=actor)

    # --- cashier shifts ---
    def cashier_shifts(self) -> QuerySet[CashierShift]:
        return self._shift.query()

    def cashier_shift(self, pk: int) -> CashierShift | None:
        return self._shift.get(pk)

    def open_cashier_shift(self, *, cashier, branch, opening_cash_uzs, notes: str) -> CashierShift:
        return domain.open_cashier_shift(
            cashier=cashier, branch=branch, opening_cash_uzs=opening_cash_uzs, notes=notes
        )

    def close_cashier_shift(self, *, shift: CashierShift, closing_cash_uzs, notes: str) -> CashierShift:
        return domain.close_cashier_shift(shift=shift, closing_cash_uzs=closing_cash_uzs, notes=notes)

    def cashier_shift_report(self, *, shift: CashierShift) -> dict:
        return selectors.cashier_shift_report(shift=shift)

    # --- outstanding ---
    def outstanding(self, *, student_id: int, user, roles: set[str]) -> tuple[Any, QuerySet[Invoice]]:
        invoices = selectors.outstanding_invoices(student_id=student_id, user=user, roles=roles)
        balance = selectors.outstanding_balance(student_id)
        return balance, invoices

    def parent_can_see_student(self, *, user, student_id: int) -> bool:
        return selectors.parent_can_see_student(user=user, student_id=student_id)
