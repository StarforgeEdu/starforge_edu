"""Payroll application service over the repository and transactional domain."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.payroll.dto import (
    AdjustmentCreateDTO,
    ExportCreateDTO,
    PaymentReconciliationDTO,
    PayrollPeriodCreateDTO,
    PreviewFilterDTO,
    ReversalDTO,
)
from apps.payroll.interfaces.repositories import IPayrollRepository
from apps.payroll.interfaces.services import IPayrollService
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
from apps.payroll.services import (
    approve_period,
    create_adjustment,
    create_period,
    decide_adjustment,
    preview_period,
    reconcile_payment,
    reject_period,
    request_export,
    reverse_payment,
    run_period,
)
from core.role_principals import RolePrincipal


class PayrollService(IPayrollService):
    def __init__(self, repository: IPayrollRepository) -> None:
        self.repository = repository

    def periods(self, *, roles, permission: str, is_superuser: bool = False) -> QuerySet[PayrollPeriod]:
        return self.repository.scoped_periods(roles=roles, permission=permission, is_superuser=is_superuser)

    def period(
        self,
        *,
        roles,
        permission: str,
        period_id: int,
        is_superuser: bool = False,
    ) -> PayrollPeriod | None:
        return self.repository.scoped_period(
            roles=roles,
            permission=permission,
            period_id=period_id,
            is_superuser=is_superuser,
        )

    def create_period(
        self, *, dto: PayrollPeriodCreateDTO, actor, principal: RolePrincipal, roles
    ) -> PayrollPeriod:
        return create_period(dto=dto, actor=actor, principal=principal, roles=roles)

    def preview(self, *, period: PayrollPeriod, filters: PreviewFilterDTO) -> dict:
        return preview_period(period=period, filters=filters)

    def run(
        self,
        *,
        period: PayrollPeriod,
        filters: PreviewFilterDTO,
        actor,
        principal: RolePrincipal,
        idempotency_key: str,
    ) -> PayrollPeriod:
        return run_period(
            period=period,
            filters=filters,
            actor=actor,
            principal=principal,
            idempotency_key=idempotency_key,
        )

    def approve(
        self,
        *,
        period: PayrollPeriod,
        actor,
        principal: RolePrincipal,
        note: str,
        idempotency_key: str,
    ) -> PayrollPeriod:
        return approve_period(
            period=period,
            actor=actor,
            principal=principal,
            note=note,
            idempotency_key=idempotency_key,
        )

    def reject(
        self,
        *,
        period: PayrollPeriod,
        actor,
        principal: RolePrincipal,
        note: str,
        idempotency_key: str,
    ) -> PayrollPeriod:
        return reject_period(
            period=period,
            actor=actor,
            principal=principal,
            note=note,
            idempotency_key=idempotency_key,
        )

    def lines(self, *, period: PayrollPeriod) -> QuerySet[PayrollLineItem]:
        return self.repository.lines(period=period)

    def payable_lines(
        self,
        *,
        roles,
        permission: str,
        is_superuser: bool = False,
    ) -> QuerySet[PayrollLineItem]:
        return self.repository.payable_lines(
            roles=roles,
            permission=permission,
            is_superuser=is_superuser,
        )

    def period_events(self, *, period: PayrollPeriod) -> QuerySet[PayrollPeriodEvent]:
        return self.repository.period_events(period=period)

    def adjustments(
        self, *, roles, permission: str, is_superuser: bool = False
    ) -> QuerySet[PayrollAdjustment]:
        return self.repository.adjustments(roles=roles, permission=permission, is_superuser=is_superuser)

    def adjustment(
        self,
        *,
        roles,
        permission: str,
        adjustment_id: int,
        is_superuser: bool = False,
    ) -> PayrollAdjustment | None:
        return self.repository.adjustment(
            roles=roles,
            permission=permission,
            adjustment_id=adjustment_id,
            is_superuser=is_superuser,
        )

    def adjustment_events(self, *, adjustment: PayrollAdjustment) -> QuerySet[PayrollAdjustmentEvent]:
        return self.repository.adjustment_events(adjustment=adjustment)

    def create_adjustment(
        self, *, dto: AdjustmentCreateDTO, actor, principal: RolePrincipal, roles
    ) -> PayrollAdjustment:
        return create_adjustment(dto=dto, actor=actor, principal=principal, roles=roles)

    def decide_adjustment(
        self,
        *,
        adjustment: PayrollAdjustment,
        approve: bool,
        actor,
        principal: RolePrincipal,
        note: str,
        idempotency_key: str,
    ) -> PayrollAdjustment:
        return decide_adjustment(
            adjustment=adjustment,
            approve=approve,
            actor=actor,
            principal=principal,
            note=note,
            idempotency_key=idempotency_key,
        )

    def reconcile_payment(
        self,
        *,
        period: PayrollPeriod,
        dto: PaymentReconciliationDTO,
        actor,
        principal: RolePrincipal,
    ) -> PayrollReconciliation:
        return reconcile_payment(period=period, dto=dto, actor=actor, principal=principal)

    def reverse_payment(
        self,
        *,
        reconciliation: PayrollReconciliation,
        dto: ReversalDTO,
        actor,
        principal: RolePrincipal,
    ) -> PayrollReconciliation:
        return reverse_payment(
            reconciliation=reconciliation,
            dto=dto,
            actor=actor,
            principal=principal,
        )

    def reconciliation(
        self,
        *,
        roles,
        permission: str,
        reconciliation_id: int,
        is_superuser: bool = False,
    ) -> PayrollReconciliation | None:
        return self.repository.reconciliation(
            roles=roles,
            permission=permission,
            reconciliation_id=reconciliation_id,
            is_superuser=is_superuser,
        )

    def reconciliations(self, *, period: PayrollPeriod) -> QuerySet[PayrollReconciliation]:
        return self.repository.reconciliations(period=period)

    def self_payslips(self, *, teacher_id: int) -> QuerySet[PayrollPayslip]:
        return self.repository.teacher_payslips(teacher_id=teacher_id)

    def self_payslip(self, *, teacher_id: int, payslip_id: int) -> PayrollPayslip | None:
        return self.repository.teacher_payslip(teacher_id=teacher_id, payslip_id=payslip_id)

    def payslip_for_reader(
        self,
        *,
        roles,
        permission: str,
        payslip_id: int,
        is_superuser: bool = False,
    ) -> PayrollPayslip | None:
        return self.repository.payslip_for_reader(
            roles=roles,
            permission=permission,
            payslip_id=payslip_id,
            is_superuser=is_superuser,
        )

    def request_export(
        self,
        *,
        period: PayrollPeriod,
        dto: ExportCreateDTO,
        actor,
        principal: RolePrincipal,
    ) -> PayrollExport:
        return request_export(period=period, dto=dto, actor=actor, principal=principal)

    def exports(self, *, period: PayrollPeriod) -> QuerySet[PayrollExport]:
        return self.repository.exports(period=period)

    def export(self, *, period: PayrollPeriod, export_id: int) -> PayrollExport | None:
        return self.repository.export(period=period, export_id=export_id)
