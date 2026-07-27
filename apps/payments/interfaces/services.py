"""Payments-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet

from apps.payments.models import Payment, ProviderConfig


class IProviderConfigService(ABC):
    @abstractmethod
    def list_configs(self) -> QuerySet[ProviderConfig]: ...

    @abstractmethod
    def get(self, *, pk: int) -> ProviderConfig | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any]) -> ProviderConfig: ...

    @abstractmethod
    def update(self, cfg: ProviderConfig, *, changes: dict[str, Any]) -> ProviderConfig: ...

    @abstractmethod
    def delete(self, cfg: ProviderConfig) -> None: ...


class IPaymentService(ABC):
    @abstractmethod
    def list_payments(self) -> QuerySet[Payment]: ...

    @abstractmethod
    def get(self, *, pk: int) -> Payment | None: ...

    @abstractmethod
    def checkout(self, *, invoice_id: int, provider: str, idempotency_key: str, payer) -> dict[str, Any]: ...

    @abstractmethod
    def cash(
        self, *, invoice_id: int, cashier, amount_uzs: Decimal | None, idempotency_key: str | None = None
    ) -> Payment: ...

    @abstractmethod
    def allocate(self, *, payment_id: int, allocations: list[dict[str, Any]]) -> Payment: ...

    @abstractmethod
    def refund(self, *, payment_id: int, amount_uzs: Decimal | None, reason: str) -> Payment: ...

    @abstractmethod
    def reconciliation(self, *, on: date) -> dict[str, Any]: ...
