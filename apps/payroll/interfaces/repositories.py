from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

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


class IPayrollRepository(ABC):
    @abstractmethod
    def scoped_periods(
        self, *, roles, permission: str, is_superuser: bool = False
    ) -> QuerySet[PayrollPeriod]: ...

    @abstractmethod
    def scoped_period(
        self, *, roles, permission: str, period_id: int, is_superuser: bool = False
    ) -> PayrollPeriod | None: ...

    @abstractmethod
    def lines(self, *, period: PayrollPeriod) -> QuerySet[PayrollLineItem]: ...

    @abstractmethod
    def payable_lines(
        self,
        *,
        roles,
        permission: str,
        is_superuser: bool = False,
    ) -> QuerySet[PayrollLineItem]: ...

    @abstractmethod
    def period_events(self, *, period: PayrollPeriod) -> QuerySet[PayrollPeriodEvent]: ...

    @abstractmethod
    def adjustments(
        self, *, roles, permission: str, is_superuser: bool = False
    ) -> QuerySet[PayrollAdjustment]: ...

    @abstractmethod
    def adjustment(
        self, *, roles, permission: str, adjustment_id: int, is_superuser: bool = False
    ) -> PayrollAdjustment | None: ...

    @abstractmethod
    def adjustment_events(self, *, adjustment: PayrollAdjustment) -> QuerySet[PayrollAdjustmentEvent]: ...

    @abstractmethod
    def teacher_payslips(self, *, teacher_id: int) -> QuerySet[PayrollPayslip]: ...

    @abstractmethod
    def teacher_payslip(self, *, teacher_id: int, payslip_id: int) -> PayrollPayslip | None: ...

    @abstractmethod
    def payslip_for_reader(
        self, *, roles, permission: str, payslip_id: int, is_superuser: bool = False
    ) -> PayrollPayslip | None: ...

    @abstractmethod
    def reconciliation(
        self, *, roles, permission: str, reconciliation_id: int, is_superuser: bool = False
    ) -> PayrollReconciliation | None: ...

    @abstractmethod
    def reconciliations(self, *, period: PayrollPeriod) -> QuerySet[PayrollReconciliation]: ...

    @abstractmethod
    def exports(self, *, period: PayrollPeriod) -> QuerySet[PayrollExport]: ...

    @abstractmethod
    def export(self, *, period: PayrollPeriod, export_id: int) -> PayrollExport | None: ...
