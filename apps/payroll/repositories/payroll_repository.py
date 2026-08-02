"""ORM repository for payroll reads and lock acquisition."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Case, DecimalField, F, QuerySet, Sum, Value, When
from django.db.models.functions import Coalesce

from apps.payroll.interfaces.repositories import IPayrollRepository
from apps.payroll.models import (
    PayrollAdjustment,
    PayrollAdjustmentEvent,
    PayrollExport,
    PayrollLineItem,
    PayrollPayslip,
    PayrollPeriod,
    PayrollPeriodEvent,
    PayrollReconciliation,
)
from core.scoping import permission_membership_scope_q

_MONEY = DecimalField(max_digits=18, decimal_places=2)


def _scope(
    queryset: QuerySet,
    *,
    roles,
    permission: str,
    branch_field: str,
    department_field: str,
    is_superuser: bool,
) -> QuerySet:
    if is_superuser:
        return queryset
    return queryset.filter(
        permission_membership_scope_q(
            roles=roles,
            permission=permission,
            branch_field=branch_field,
            department_field=department_field,
        )
    )


class PayrollRepository(IPayrollRepository):
    def _periods(self) -> QuerySet[PayrollPeriod]:
        return PayrollPeriod.objects.select_related(
            "branch",
            "department",
            "correction_of",
            "created_by",
            "run_by",
            "approved_by",
            "rejected_by",
        )

    def scoped_periods(
        self, *, roles, permission: str, is_superuser: bool = False
    ) -> QuerySet[PayrollPeriod]:
        return _scope(
            self._periods(),
            roles=roles,
            permission=permission,
            branch_field="branch_id",
            department_field="department_id",
            is_superuser=is_superuser,
        )

    def scoped_period(
        self,
        *,
        roles,
        permission: str,
        period_id: int,
        is_superuser: bool = False,
    ) -> PayrollPeriod | None:
        return (
            self.scoped_periods(roles=roles, permission=permission, is_superuser=is_superuser)
            .filter(pk=period_id)
            .first()
        )

    def _lines(self) -> QuerySet[PayrollLineItem]:
        paid = Coalesce(
            Sum(
                Case(
                    When(
                        reconciliations__kind=PayrollReconciliation.Kind.PAYMENT,
                        then=F("reconciliations__amount_uzs"),
                    ),
                    When(
                        reconciliations__kind=PayrollReconciliation.Kind.REVERSAL,
                        then=-F("reconciliations__amount_uzs"),
                    ),
                    default=Value(Decimal("0")),
                    output_field=_MONEY,
                )
            ),
            Value(Decimal("0")),
            output_field=_MONEY,
        )
        return (
            PayrollLineItem.objects.all()
            .select_related(
                "period",
                "teacher",
                "teacher__user",
                "branch_at_run",
                "department_at_run",
                "payslip",
            )
            .annotate(paid_amount_uzs=paid)
            .annotate(outstanding_amount_uzs=F("net_amount_uzs") - F("paid_amount_uzs"))
        )

    def lines(self, *, period: PayrollPeriod) -> QuerySet[PayrollLineItem]:
        return self._lines().filter(period=period)

    def payable_lines(
        self,
        *,
        roles,
        permission: str,
        is_superuser: bool = False,
    ) -> QuerySet[PayrollLineItem]:
        queryset = self._lines().filter(
            period__status__in=(
                PayrollPeriod.Status.APPROVED,
                PayrollPeriod.Status.PAYMENT_IN_PROGRESS,
            ),
            # Correlated annotation installed by ``_lines``; django-stubs only
            # knows concrete PayrollLineItem fields.
            outstanding_amount_uzs__gt=0,  # type: ignore[misc]
        )
        return _scope(
            queryset,
            roles=roles,
            permission=permission,
            branch_field="branch_at_run_id",
            department_field="department_at_run_id",
            is_superuser=is_superuser,
        )

    def period_events(self, *, period: PayrollPeriod) -> QuerySet[PayrollPeriodEvent]:
        return PayrollPeriodEvent.objects.filter(period=period).select_related("actor")

    def _adjustments(self) -> QuerySet[PayrollAdjustment]:
        return PayrollAdjustment.objects.select_related(
            "teacher",
            "teacher__user",
            "branch",
            "department",
            "created_by",
            "decided_by",
            "applied_line",
            "applied_line__period",
        )

    def adjustments(
        self, *, roles, permission: str, is_superuser: bool = False
    ) -> QuerySet[PayrollAdjustment]:
        return _scope(
            self._adjustments(),
            roles=roles,
            permission=permission,
            branch_field="branch_id",
            department_field="department_id",
            is_superuser=is_superuser,
        )

    def adjustment(
        self,
        *,
        roles,
        permission: str,
        adjustment_id: int,
        is_superuser: bool = False,
    ) -> PayrollAdjustment | None:
        return (
            self.adjustments(roles=roles, permission=permission, is_superuser=is_superuser)
            .filter(pk=adjustment_id)
            .first()
        )

    def adjustment_events(self, *, adjustment: PayrollAdjustment) -> QuerySet[PayrollAdjustmentEvent]:
        return PayrollAdjustmentEvent.objects.filter(adjustment=adjustment).select_related("actor")

    def teacher_payslips(self, *, teacher_id: int) -> QuerySet[PayrollPayslip]:
        visible_states = (
            PayrollPeriod.Status.APPROVED,
            PayrollPeriod.Status.PAYMENT_IN_PROGRESS,
            PayrollPeriod.Status.PAID,
        )
        return (
            PayrollPayslip.objects.filter(
                line_item__teacher_id=teacher_id,
                line_item__period__status__in=visible_states,
            )
            .select_related(
                "line_item",
                "line_item__period",
                "line_item__branch_at_run",
                "line_item__department_at_run",
            )
            .order_by("-line_item__period__period_end", "-id")
        )

    def teacher_payslip(self, *, teacher_id: int, payslip_id: int) -> PayrollPayslip | None:
        return self.teacher_payslips(teacher_id=teacher_id).filter(pk=payslip_id).first()

    def payslip_for_reader(
        self,
        *,
        roles,
        permission: str,
        payslip_id: int,
        is_superuser: bool = False,
    ) -> PayrollPayslip | None:
        queryset = PayrollPayslip.objects.select_related(
            "line_item",
            "line_item__period",
            "line_item__branch_at_run",
            "line_item__department_at_run",
        )
        return (
            _scope(
                queryset,
                roles=roles,
                permission=permission,
                branch_field="line_item__branch_at_run_id",
                department_field="line_item__department_at_run_id",
                is_superuser=is_superuser,
            )
            .filter(pk=payslip_id)
            .first()
        )

    def reconciliation(
        self,
        *,
        roles,
        permission: str,
        reconciliation_id: int,
        is_superuser: bool = False,
    ) -> PayrollReconciliation | None:
        queryset = PayrollReconciliation.objects.select_related(
            "line_item",
            "line_item__period",
            "line_item__teacher",
            "payment_method",
            "ledger_entry",
            "recorded_by",
            "reverses",
        )
        return (
            _scope(
                queryset,
                roles=roles,
                permission=permission,
                branch_field="line_item__branch_at_run_id",
                department_field="line_item__department_at_run_id",
                is_superuser=is_superuser,
            )
            .filter(pk=reconciliation_id)
            .first()
        )

    def reconciliations(self, *, period: PayrollPeriod) -> QuerySet[PayrollReconciliation]:
        return PayrollReconciliation.objects.filter(line_item__period=period).select_related(
            "line_item",
            "line_item__period",
            "line_item__teacher",
            "payment_method",
            "ledger_entry",
            "recorded_by",
            "reverses",
        )

    def exports(self, *, period: PayrollPeriod) -> QuerySet[PayrollExport]:
        return PayrollExport.objects.filter(period=period).select_related("period", "requested_by")

    def export(self, *, period: PayrollPeriod, export_id: int) -> PayrollExport | None:
        return self.exports(period=period).filter(pk=export_id).first()
