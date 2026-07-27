"""F9-2 — metered AI-overage billing.

AI generation beyond the plan's monthly token allowance is charged per use: the
nightly meter records an `AiUsageCharge` per (Center, billing month) where
`amount_uzs = overage_tokens / 1000 * plan.ai_overage_price_per_1k_uzs`. Cross-schema:
the Subscription/Plan + charge live on the public schema, the token/cost totals are
read inside the Center's tenant schema. Idempotent per month (re-metered in place).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.billing.models import AiUsageCharge, Subscription
from apps.billing.tests.factories import PlanFactory

pytestmark = pytest.mark.django_db


def _subscribe(center, *, allowance, rate):
    plan = PlanFactory(ai_tokens_month=allowance, ai_overage_price_per_1k_uzs=Decimal(str(rate)))
    Subscription.objects.update_or_create(
        center=center,
        defaults={
            "plan": plan,
            "status": Subscription.Status.ACTIVE,
            "current_period_start": timezone.now() - timedelta(days=1),
            "current_period_end": timezone.now() + timedelta(days=30),
        },
    )
    return plan


def _ai_usage(center, *, n_requests, cost_each=0):
    """`n_requests` succeeded AI requests in the tenant schema (150 tokens each)."""
    from apps.ai.tests.factories import AIRequestFactory

    with schema_context(center.schema_name):
        for _ in range(n_requests):
            AIRequestFactory.create(cost_microusd=cost_each)


def test_no_charge_when_usage_is_within_the_plan_allowance(tenant_a):
    _subscribe(tenant_a, allowance=100_000, rate=1000)
    _ai_usage(tenant_a, n_requests=10)  # 1500 tokens << 100k allowance
    from apps.billing.services import meter_ai_overage

    charge = meter_ai_overage(center_id=tenant_a.pk)
    assert charge.overage_tokens == 0
    assert charge.amount_uzs == Decimal("0.00")
    assert charge.used_tokens == 1500


def test_overage_is_charged_at_the_plan_rate(tenant_a):
    _subscribe(tenant_a, allowance=1000, rate=1000)  # 1000 UZS per 1000 overage tokens
    _ai_usage(tenant_a, n_requests=10, cost_each=200)  # 1500 tokens, 2000 microUSD
    from apps.billing.services import meter_ai_overage

    charge = meter_ai_overage(center_id=tenant_a.pk)
    assert charge.used_tokens == 1500
    assert charge.included_tokens == 1000
    assert charge.overage_tokens == 500
    assert charge.amount_uzs == Decimal("500.00")  # 500/1000 * 1000
    assert charge.cost_microusd == 2000  # provider cost recorded for reconciliation


def test_zero_rate_records_overage_but_bills_nothing(tenant_a):
    """A plan that meters but does not bill AI overage (rate 0) still records the
    overage tokens for visibility, but the charged amount stays 0."""
    _subscribe(tenant_a, allowance=1000, rate=0)
    _ai_usage(tenant_a, n_requests=10)  # 1500 tokens
    from apps.billing.services import meter_ai_overage

    charge = meter_ai_overage(center_id=tenant_a.pk)
    assert charge.overage_tokens == 500
    assert charge.amount_uzs == Decimal("0.00")


def test_metering_is_idempotent_per_month(tenant_a):
    _subscribe(tenant_a, allowance=1000, rate=1000)
    _ai_usage(tenant_a, n_requests=10)
    from apps.billing.services import meter_ai_overage

    meter_ai_overage(center_id=tenant_a.pk)
    meter_ai_overage(center_id=tenant_a.pk)
    period = timezone.localdate().replace(day=1)  # match the service's local-month bucket
    rows = AiUsageCharge.objects.filter(center=tenant_a, period=period)
    assert rows.count() == 1  # re-metered in place, never duplicated


def test_charge_grows_as_usage_accrues_within_the_month(tenant_a):
    _subscribe(tenant_a, allowance=1000, rate=1000)
    _ai_usage(tenant_a, n_requests=10)  # 1500 tokens -> overage 500
    from apps.billing.services import meter_ai_overage

    first = meter_ai_overage(center_id=tenant_a.pk)
    assert first.overage_tokens == 500
    _ai_usage(tenant_a, n_requests=10)  # +1500 -> 3000 total, overage 2000
    second = meter_ai_overage(center_id=tenant_a.pk)
    assert second.pk == first.pk  # same month row
    assert second.overage_tokens == 2000
    assert second.amount_uzs == Decimal("2000.00")


def test_no_charge_for_a_center_without_a_subscription(tenant_a):
    # no _subscribe() call -> the receiver may have made a trial sub; remove it
    Subscription.objects.filter(center=tenant_a).delete()
    from apps.billing.services import meter_ai_overage

    assert meter_ai_overage(center_id=tenant_a.pk) is None


def test_charge_period_and_usage_window_agree_at_month_boundary(tenant_a):
    """Regression: the charge period and the token window must come from ONE local date.
    At UTC 22:00 on Dec 31 it is already Jan 1 in Asia/Tashkent (UTC+5). A UTC-derived
    period (Dec) with a local-month token window (Jan) would overwrite December's
    finalized charge with January's running total. Both must resolve to the local month."""
    _subscribe(tenant_a, allowance=1000, rate=1000)
    # UTC 2025-12-31 22:00 == local 2026-01-01 03:00 (Asia/Tashkent, UTC+5).
    boundary = datetime(2025, 12, 31, 22, 0, tzinfo=UTC)
    with time_machine.travel(boundary, tick=False):
        _ai_usage(tenant_a, n_requests=10)  # 1500 tokens, stamped at the frozen instant
        from apps.billing.services import meter_ai_overage

        charge = meter_ai_overage(center_id=tenant_a.pk)
    # period is the LOCAL month (January 2026), not the UTC month (December 2025)
    from datetime import date

    assert charge.period == date(2026, 1, 1)
    # and the usage window catches the boundary tokens under that same month
    assert charge.used_tokens == 1500
    assert charge.overage_tokens == 500


def test_nightly_meter_records_the_overage_charge(tenant_a):
    """The overage charge is wired into the nightly per-center meter, so it is produced
    by the same run that snapshots usage and evaluates subscription state."""
    _subscribe(tenant_a, allowance=1000, rate=1000)
    _ai_usage(tenant_a, n_requests=10)
    from celery_tasks.billing_tasks import meter_center

    meter_center(center_id=tenant_a.pk)
    charge = AiUsageCharge.objects.get(center=tenant_a, period=timezone.localdate().replace(day=1))
    assert charge.overage_tokens == 500
    assert charge.amount_uzs == Decimal("500.00")
