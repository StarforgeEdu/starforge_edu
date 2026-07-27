"""Payments application services (provider-config CRUD + delegation to the
preserved payment/checkout/allocation/refund domain functions)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from django.db.models import QuerySet

from apps.payments import selectors
from apps.payments import services as domain
from apps.payments.interfaces.repositories import IPaymentRepository, IProviderConfigRepository
from apps.payments.interfaces.services import IPaymentService, IProviderConfigService
from apps.payments.models import Payment, ProviderConfig


class ProviderConfigService(IProviderConfigService):
    def __init__(self, repository: IProviderConfigRepository) -> None:
        self.repository = repository

    def list_configs(self) -> QuerySet[ProviderConfig]:
        return self.repository.list_configs()

    def get(self, *, pk: int) -> ProviderConfig | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> ProviderConfig:
        return self.repository.add(data=data)

    def update(self, cfg: ProviderConfig, *, changes: dict[str, Any]) -> ProviderConfig:
        return self.repository.apply_changes(cfg, changes=changes)

    def delete(self, cfg: ProviderConfig) -> None:
        self.repository.remove(cfg)


class PaymentService(IPaymentService):
    def __init__(self, repository: IPaymentRepository) -> None:
        self.repository = repository

    def list_payments(self) -> QuerySet[Payment]:
        return self.repository.scoped()

    def get(self, *, pk: int) -> Payment | None:
        return self.repository.get(pk=pk)

    def checkout(self, *, invoice_id: int, provider: str, idempotency_key: str, payer) -> dict[str, Any]:
        return domain.create_checkout(
            invoice_id=invoice_id, provider=provider, idempotency_key=idempotency_key, payer=payer
        )

    def cash(
        self, *, invoice_id: int, cashier, amount_uzs: Decimal | None, idempotency_key: str | None = None
    ) -> Payment:
        return domain.create_cash_payment(
            invoice_id=invoice_id, cashier=cashier, amount_uzs=amount_uzs, idempotency_key=idempotency_key
        )

    def allocate(self, *, payment_id: int, allocations: list[dict[str, Any]]) -> Payment:
        return domain.allocate_manual(payment_id=payment_id, allocations=allocations)

    def refund(self, *, payment_id: int, amount_uzs: Decimal | None, reason: str) -> Payment:
        return domain.refund_payment(payment_id=payment_id, amount_uzs=amount_uzs, reason=reason)

    def reconciliation(self, *, on: date) -> dict[str, Any]:
        return selectors.reconciliation(on=on)
