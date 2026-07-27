"""Budget reserve/record + rollover tests (D4-LA-4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import time_machine
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.ai.models import AIRequest, TenantAIBudget
from apps.ai.services import (
    AIBudgetExceeded,
    Usage,
    check_and_reserve_budget,
    cost_microusd,
    record_usage,
)
from apps.ai.tests.factories import AIPromptFactory, make_budget

pytestmark = pytest.mark.django_db


def _seed(tenant):
    with schema_context(tenant.schema_name):
        AIPromptFactory()


def test_reserve_creates_queued_request(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000, monthly_token_limit=100_000)
        req = check_and_reserve_budget(
            feature="assignment_feedback",
            estimated_tokens=500,
            source_app="assignments",
            source_id=1,
        )
        assert req.status == AIRequest.Status.QUEUED
        assert req.idempotency_key == "assignment_feedback:assignments:1:v1"


def test_over_daily_budget_denies_and_records(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=100, monthly_token_limit=100_000)
        with pytest.raises(AIBudgetExceeded) as exc:
            check_and_reserve_budget(
                feature="assignment_feedback",
                estimated_tokens=500,
                source_app="assignments",
                source_id=2,
            )
        assert exc.value.code == "ai_budget_exceeded"
        assert exc.value.status_code == 429
        denied = AIRequest.objects.get(source_id=2)
        assert denied.status == AIRequest.Status.DENIED_BUDGET


def test_over_monthly_budget_denies(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=100_000, monthly_token_limit=100)
        with pytest.raises(AIBudgetExceeded):
            check_and_reserve_budget(
                feature="assignment_feedback",
                estimated_tokens=500,
                source_app="assignments",
                source_id=3,
            )


def test_disabled_budget_denies(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(is_enabled=False)
        with pytest.raises(AIBudgetExceeded):
            check_and_reserve_budget(
                feature="assignment_feedback",
                estimated_tokens=1,
                source_app="assignments",
                source_id=4,
            )


def test_reserve_is_idempotent(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000)
        r1 = check_and_reserve_budget(
            feature="assignment_feedback", estimated_tokens=10, source_app="assignments", source_id=5
        )
        r2 = check_and_reserve_budget(
            feature="assignment_feedback", estimated_tokens=10, source_app="assignments", source_id=5
        )
        assert r1.pk == r2.pk
        assert AIRequest.objects.filter(source_id=5).count() == 1


def test_record_usage_bumps_counters_atomically(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000, monthly_token_limit=100_000)
        req = check_and_reserve_budget(
            feature="assignment_feedback", estimated_tokens=10, source_app="assignments", source_id=6
        )
        req.status = AIRequest.Status.RUNNING
        req.save(update_fields=["status"])
        record_usage(ai_request_id=req.pk, usage=Usage(input_tokens=120, output_tokens=80))
        budget = TenantAIBudget.objects.get(pk=1)
        assert budget.tokens_used_today == 200
        assert budget.tokens_used_month == 200
        req.refresh_from_db()
        assert req.input_tokens == 120
        assert req.output_tokens == 80


def test_record_usage_no_double_count_on_terminal(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000)
        req = check_and_reserve_budget(
            feature="assignment_feedback", estimated_tokens=10, source_app="assignments", source_id=7
        )
        req.status = AIRequest.Status.RUNNING
        req.save(update_fields=["status"])
        usage = Usage(input_tokens=100, output_tokens=100)
        record_usage(ai_request_id=req.pk, usage=usage)
        req.refresh_from_db()
        req.status = AIRequest.Status.SUCCEEDED
        req.save(update_fields=["status"])
        # A retry after success must not double-count.
        record_usage(ai_request_id=req.pk, usage=usage)
        budget = TenantAIBudget.objects.get(pk=1)
        assert budget.tokens_used_today == 200


def test_day_anchor_rolls_over(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        budget = make_budget(daily_token_limit=10_000, monthly_token_limit=1_000_000)
        budget.tokens_used_today = 9_000
        budget.day_anchor = timezone.localdate() - timedelta(days=1)
        budget.save()
        # A new reservation the next day must see the counter reset to 0 first,
        # then reserve its estimate against the fresh day. (Rollover proof: without
        # the reset, 9000+5000 would exceed the 10000 cap and be DENIED, leaving
        # the counter at 9000 — so landing on exactly the 5000 reservation shows
        # the day rolled over.)
        check_and_reserve_budget(
            feature="assignment_feedback", estimated_tokens=5_000, source_app="assignments", source_id=8
        )
        budget.refresh_from_db()
        assert budget.tokens_used_today == 5_000  # reset to 0, then 5000 reserved
        assert budget.day_anchor == timezone.localdate()


def test_month_anchor_rolls_over():
    # Travel across a month boundary; the monthly counter resets.
    import apps.tenancy.models  # noqa: F401  (ensure app registry ready)

    with time_machine.travel("2026-02-15", tick=False), schema_context("tenant_a"):
        AIPromptFactory()
        budget = make_budget(daily_token_limit=1_000_000, monthly_token_limit=10_000)
        budget.tokens_used_month = 9_000
        budget.month_anchor = timezone.localdate()
        budget.save()
    with time_machine.travel("2026-03-01", tick=False), schema_context("tenant_a"):
        check_and_reserve_budget(
            feature="assignment_feedback",
            estimated_tokens=5_000,
            source_app="assignments",
            source_id=9,
        )
        budget = TenantAIBudget.objects.get(pk=1)
        # Reset to 0 on the month rollover, then 5000 reserved (without the reset,
        # 9000+5000 would exceed the 10000 monthly cap and be denied → stay 9000).
        assert budget.tokens_used_month == 5_000


def test_cost_microusd_uses_settings():
    # 1M input + 1M output at default placeholder prices = 3M + 15M microUSD.
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_microusd(usage) == 3_000_000 + 15_000_000
