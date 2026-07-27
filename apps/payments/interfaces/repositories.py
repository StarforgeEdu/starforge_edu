"""Payments-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.payments.models import Payment, ProviderConfig
from core.interfaces import IBaseRepository


class IProviderConfigRepository(IBaseRepository[ProviderConfig]):
    def list_configs(self) -> QuerySet[ProviderConfig]:
        raise NotImplementedError

    def get(self, *, pk: int) -> ProviderConfig | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> ProviderConfig:
        raise NotImplementedError

    def apply_changes(self, cfg: ProviderConfig, *, changes: dict[str, Any]) -> ProviderConfig:
        raise NotImplementedError

    def remove(self, cfg: ProviderConfig) -> None:
        raise NotImplementedError


class IPaymentRepository(IBaseRepository[Payment]):
    def scoped(self) -> QuerySet[Payment]:
        raise NotImplementedError

    def get(self, *, pk: int) -> Payment | None:
        raise NotImplementedError
