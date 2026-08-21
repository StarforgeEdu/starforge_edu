"""Payments application services (provider-config CRUD + delegation to the
preserved payment/checkout/allocation/refund domain functions)."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.payments import selectors
from apps.payments import services as domain
from apps.payments.interfaces.repositories import IPaymentRepository, IProviderConfigRepository
from apps.payments.interfaces.services import IPaymentService, IProviderConfigService
from apps.payments.models import Payment, ProviderConfig
from core.exceptions import ValidationException

_SAFE_PROVIDER_IDENTIFIER = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
_PROVIDER_REQUIRED_FIELDS = {
    "click": ("click_service_id", "click_merchant_id", "click_secret_key"),
    "payme": ("payme_merchant_id", "payme_key"),
    "uzum": ("uzum_merchant_id", "uzum_api_key"),
}
_PROVIDER_IDENTIFIER_FIELDS = {
    "click": ("click_service_id", "click_merchant_id"),
    "payme": ("payme_merchant_id",),
    "uzum": ("uzum_merchant_id",),
}
_PROVIDER_OPTIONAL_SECRET_FIELDS = {
    "click": (),
    "payme": ("payme_test_key",),
    "uzum": (),
}


def _validate_config_state(*, current: ProviderConfig | None, changes: dict[str, Any]) -> None:
    provider = changes.get("provider", getattr(current, "provider", ""))
    active = changes.get("is_active", getattr(current, "is_active", True))
    if provider not in _PROVIDER_REQUIRED_FIELDS:
        raise ValidationException(
            _("Invalid provider configuration."),
            code="validation_error",
            fields={"provider": [_("Invalid provider.")]},
        )
    if provider == "uzum" and active and not getattr(settings, "UZUM_LEGACY_INTEGRATION_ENABLED", False):
        raise ValidationException(
            _("This provider contract is not available."),
            code="provider_contract_unavailable",
            fields={"is_active": [_("Keep this provider inactive.")]},
        )
    provider_fields = dict.fromkeys(
        (*_PROVIDER_REQUIRED_FIELDS[provider], *_PROVIDER_OPTIONAL_SECRET_FIELDS[provider])
    )
    values = {field: changes.get(field, getattr(current, field, "")) for field in provider_fields}
    invalid_identifiers = [
        field
        for field in _PROVIDER_IDENTIFIER_FIELDS[provider]
        if values.get(field) and _SAFE_PROVIDER_IDENTIFIER.fullmatch(str(values[field])) is None
    ]
    if invalid_identifiers:
        raise ValidationException(
            _("Invalid provider configuration."),
            code="validation_error",
            fields={field: [_("Invalid value.")] for field in invalid_identifiers},
        )
    invalid_credentials = [
        field
        for field, value in values.items()
        if field not in _PROVIDER_IDENTIFIER_FIELDS[provider]
        and value
        and (
            not isinstance(value, str)
            or len(value) > 255
            or value.strip() != value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        )
    ]
    if invalid_credentials:
        raise ValidationException(
            _("Invalid provider configuration."),
            code="validation_error",
            fields={field: [_("Invalid value.")] for field in invalid_credentials},
        )
    if active:
        missing = [
            field
            for field in _PROVIDER_REQUIRED_FIELDS[provider]
            if not isinstance(values[field], str) or not values[field].strip()
        ]
        if missing:
            raise ValidationException(
                _("An active provider configuration requires complete credentials."),
                code="provider_config_incomplete",
                fields={field: [_("This field is required.")] for field in missing},
            )


class ProviderConfigService(IProviderConfigService):
    def __init__(self, repository: IProviderConfigRepository) -> None:
        self.repository = repository

    def list_configs(self) -> QuerySet[ProviderConfig]:
        return self.repository.list_configs()

    def get(self, *, pk: int) -> ProviderConfig | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> ProviderConfig:
        _validate_config_state(current=None, changes=data)
        return self.repository.add(data=data)

    def update(self, cfg: ProviderConfig, *, changes: dict[str, Any]) -> ProviderConfig:
        _validate_config_state(current=cfg, changes=changes)
        return self.repository.apply_changes(cfg, changes=changes)

    def delete(self, cfg: ProviderConfig) -> None:
        self.repository.remove(cfg)


class PaymentService(IPaymentService):
    def __init__(self, repository: IPaymentRepository) -> None:
        self.repository = repository

    def list_payments(self) -> QuerySet[Payment]:
        return self.repository.scoped()

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

    def refund(
        self, *, payment_id: int, amount_uzs: Decimal | None, reason: str, requested_by
    ) -> tuple[Payment, Any]:
        return domain.refund_payment(
            payment_id=payment_id,
            amount_uzs=amount_uzs,
            reason=reason,
            requested_by=requested_by,
        )

    def reconciliation(
        self,
        *,
        on: date,
        scope_pairs: set[tuple[int, int | None]] | None = None,
    ) -> dict[str, Any]:
        return selectors.reconciliation(on=on, scope_pairs=scope_pairs)
