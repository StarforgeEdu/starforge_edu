"""Validated payroll command/query DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PayrollPeriodCreateDTO:
    branch_id: int
    department_id: int | None
    label: str
    period_start: date
    period_end: date
    pay_date: date | None
    currency: str
    correction_of_id: int | None = None
    correction_reason: str = ""


@dataclass(frozen=True, slots=True)
class PreviewFilterDTO:
    teacher_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class AdjustmentCreateDTO:
    teacher_id: int
    kind: str
    amount_uzs: Decimal
    currency: str
    effective_period_start: date
    effective_period_end: date
    reason: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PaymentReconciliationDTO:
    line_item_id: int
    amount_uzs: Decimal
    payment_method_id: int
    external_reference: str
    paid_at: datetime
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ReversalDTO:
    external_reference: str
    paid_at: datetime
    reason: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ExportCreateDTO:
    format: str
    teacher_id: int | None
    payment_state: str | None
    idempotency_key: str
