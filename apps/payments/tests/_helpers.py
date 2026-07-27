"""Shared setup helpers for the Lane-F payment attack + golden suites.

These create the per-tenant ``ProviderConfig`` with the deterministic builder
credentials and post to the **public-schema** webhook URL (TD-6:
``POST /api/v1/webhooks/<provider>/<center_slug>/``). The webhook is public —
posted with the apex/``testserver`` host, NOT the tenant host — and the view
resolves the tenant from ``<center_slug>`` and enters ``schema_context`` itself.

Written against the DAY-3.md Lane B contract (clients/models mid-build in
parallel); imports of lane code are LAZY inside functions so collection never
hard-fails while a sibling app is being built. The orchestrator runs everything
centrally once A..E have merged.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django_tenants.utils import schema_context
from rest_framework.test import APIClient

from apps.payments.tests import builders

WEBHOOK_BASE = "/api/v1/webhooks"


def webhook_url(provider: str, center_slug: str) -> str:
    return f"{WEBHOOK_BASE}/{provider}/{center_slug}/"


def public_client() -> APIClient:
    """A client bound to the public/apex host (webhooks are public-schema)."""
    return APIClient(HTTP_HOST="testserver")


def seed_provider_configs(center) -> None:
    """Create active click/payme/uzum ProviderConfig rows in the tenant schema
    with the deterministic builder credentials, so builder signatures verify."""
    from apps.payments.models import ProviderConfig

    with schema_context(center.schema_name):
        ProviderConfig.objects.update_or_create(
            provider="click",
            defaults={
                "is_active": True,
                "click_service_id": builders.CLICK_SERVICE_ID,
                "click_merchant_id": builders.CLICK_MERCHANT_ID,
                "click_secret_key": builders.CLICK_SECRET_KEY,
            },
        )
        ProviderConfig.objects.update_or_create(
            provider="payme",
            defaults={
                "is_active": True,
                "payme_merchant_id": "payme_merchant",
                "payme_key": builders.PAYME_KEY,
                "payme_test_key": builders.PAYME_KEY,
            },
        )
        ProviderConfig.objects.update_or_create(
            provider="uzum",
            defaults={
                "is_active": True,
                "uzum_merchant_id": "uzum_merchant",
                "uzum_api_key": builders.UZUM_API_KEY,
            },
        )


def seed_open_invoice(
    center,
    *,
    number: str = "INV-2026-000001",
    amount_uzs: str = "150000.00",
):
    """Create an issued invoice the webhook account can resolve to.

    Routes through the Lane-A factory if present, else builds the row directly.
    Returns the Invoice instance (queried inside the schema by the caller).
    """
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.finance.models import Invoice
    from apps.org.tests.factories import BranchFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(center.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)
        from datetime import date

        invoice = Invoice.objects.create(
            number=number,
            student=student,
            cohort=cohort,
            status="issued",
            issue_date=date(2026, 6, 1),
            due_date=date(2026, 6, 30),
            currency="UZS",
            total_uzs=Decimal(amount_uzs),
        )
        return invoice


def payment_rows(center, **filters) -> list[Any]:
    from apps.payments.models import Payment

    with schema_context(center.schema_name):
        return list(Payment.objects.filter(**filters))


def webhook_event_rows(center, **filters) -> list[Any]:
    from apps.payments.models import WebhookEvent

    with schema_context(center.schema_name):
        return list(WebhookEvent.objects.filter(**filters))


def allocation_rows(center, **filters) -> list[Any]:
    from apps.finance.models import PaymentAllocation

    with schema_context(center.schema_name):
        return list(PaymentAllocation.objects.filter(**filters))
