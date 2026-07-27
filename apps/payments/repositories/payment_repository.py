"""ORM-backed payments repositories (reads via the preserved selectors)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.payments import selectors
from apps.payments.interfaces.repositories import IPaymentRepository, IProviderConfigRepository
from apps.payments.models import Payment, ProviderConfig
from core.repositories import BaseRepository


class ProviderConfigRepository(BaseRepository[ProviderConfig], IProviderConfigRepository):
    model = ProviderConfig

    def list_configs(self) -> QuerySet[ProviderConfig]:
        return ProviderConfig.objects.all().order_by("provider")

    def get(self, *, pk: int) -> ProviderConfig | None:
        return ProviderConfig.objects.filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> ProviderConfig:
        return ProviderConfig.objects.create(**data)

    def apply_changes(self, cfg: ProviderConfig, *, changes: dict[str, Any]) -> ProviderConfig:
        for field, value in changes.items():
            setattr(cfg, field, value)
        if changes:
            cfg.save(update_fields=[*changes.keys(), "updated_at"])
        return cfg

    def remove(self, cfg: ProviderConfig) -> None:
        cfg.delete()


class PaymentRepository(BaseRepository[Payment], IPaymentRepository):
    model = Payment

    def scoped(self) -> QuerySet[Payment]:
        return selectors.payments_qs()

    def get(self, *, pk: int) -> Payment | None:
        return selectors.payments_qs().filter(pk=pk).first()
