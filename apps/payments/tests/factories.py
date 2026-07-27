"""Payments test factories (factory-boy)."""

from __future__ import annotations

from decimal import Decimal

import factory

from apps.payments.models import FiscalReceipt, Payment, Provider, ProviderConfig


class ProviderConfigFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ProviderConfig
        django_get_or_create = ("provider",)

    provider = Provider.PAYME
    is_active = True
    payme_merchant_id = "merchant-1"
    payme_key = "payme-secret-key"
    click_service_id = "svc-1"
    click_merchant_id = "merch-1"
    click_secret_key = "click-secret"
    uzum_merchant_id = "uzum-1"
    uzum_api_key = "uzum-secret-key"


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Payment

    provider = Payment.Method.PAYME
    amount_uzs = Decimal("100000.00")
    status = Payment.Status.PENDING
    idempotency_key = factory.Sequence(lambda n: f"idem-{n}")
    account_ref = factory.Sequence(lambda n: f"INV-2026-{n:06d}")
    metadata = factory.LazyFunction(dict)


class FiscalReceiptFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FiscalReceipt

    payment = factory.SubFactory(PaymentFactory)
    status = FiscalReceipt.Status.PENDING
