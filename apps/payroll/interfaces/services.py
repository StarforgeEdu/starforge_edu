from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.payroll.dto import (
    AdjustmentCreateDTO,
    ExportCreateDTO,
    PaymentReconciliationDTO,
    PayrollPeriodCreateDTO,
    PreviewFilterDTO,
    ReversalDTO,
)
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
from core.role_principals import RolePrincipal


class IPayrollService(ABC):
    @abstractmethod
    def periods(self, *, roles, permission: str, is_superuser: bool = False) -> QuerySet[PayrollPeriod]: ...

    @abstractmethod
    def period(
        self, *, roles, permission: str, period_id: int, is_superuser: bool = False
    ) -> PayrollPeriod | None: ...

    @abstractmethod
    def create_period(
        self, *, dto: PayrollPeriodCreateDTO, actor, principal: RolePrincipal, roles
    ) -> PayrollPeriod: ...

    @abstractmethod
    def preview(self, *, period: PayrollPeriod, filters: PreviewFilterDTO) -> dict: ...

    @abstractmethod
    def run(
        self,
        *,
        period: PayrollPeriod,
        filters: PreviewFilterDTO,
        actor,
        principal: RolePrincipal,
        idempotency_key: str,
    ) -> PayrollPeriod: ...

    @abstractmethod
    def approve(
        self,
        *,
        period: PayrollPeriod,
        actor,
        principal: RolePrincipal,
        note: str,
        idempotency_key: str,
    ) -> PayrollPeriod: ...

    @abstractmethod
    def reject(
        self,
        *,
        period: PayrollPeriod,
        actor,
        principal: RolePrincipal,
        note: str,
        idempotency_key: str,
    ) -> PayrollPeriod: ...

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
    def create_adjustment(
        self, *, dto: AdjustmentCreateDTO, actor, principal: RolePrincipal, roles
    ) -> PayrollAdjustment: ...

    @abstractmethod
    def decide_adjustment(
        self,
        *,
        adjustment: PayrollAdjustment,
        approve: bool,
        actor,
        principal: RolePrincipal,
        note: str,
        idempotency_key: str,
    ) -> PayrollAdjustment: ...

    @abstractmethod
    def reconcile_payment(
        self,
        *,
        period: PayrollPeriod,
        dto: PaymentReconciliationDTO,
        actor,
        principal: RolePrincipal,
    ) -> PayrollReconciliation: ...

    @abstractmethod
    def reverse_payment(
        self,
        *,
        reconciliation: PayrollReconciliation,
        dto: ReversalDTO,
        actor,
        principal: RolePrincipal,
    ) -> PayrollReconciliation: ...

    @abstractmethod
    def reconciliation(
        self,
        *,
        roles,
        permission: str,
        reconciliation_id: int,
        is_superuser: bool = False,
    ) -> PayrollReconciliation | None: ...

    @abstractmethod
    def reconciliations(self, *, period: PayrollPeriod) -> QuerySet[PayrollReconciliation]: ...

    @abstractmethod
    def self_payslips(self, *, teacher_id: int) -> QuerySet[PayrollPayslip]: ...

    @abstractmethod
    def self_payslip(self, *, teacher_id: int, payslip_id: int) -> PayrollPayslip | None: ...

    @abstractmethod
    def payslip_for_reader(
        self, *, roles, permission: str, payslip_id: int, is_superuser: bool = False
    ) -> PayrollPayslip | None: ...

    @abstractmethod
    def request_export(
        self,
        *,
        period: PayrollPeriod,
        dto: ExportCreateDTO,
        actor,
        principal: RolePrincipal,
    ) -> PayrollExport: ...

    @abstractmethod
    def exports(self, *, period: PayrollPeriod) -> QuerySet[PayrollExport]: ...

    @abstractmethod
    def export(self, *, period: PayrollPeriod, export_id: int) -> PayrollExport | None: ...
