"""Budget reserve/record + rollover tests (D4-LA-4)."""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
import time_machine
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.ai.models import AIRequest, TenantAIBudget
from apps.ai.services import (
    AIBudgetExceeded,
    Usage,
    begin_provider_attempt,
    cost_microusd,
    record_provider_completion,
)
from apps.ai.services import (
    check_and_reserve_budget as _check_and_reserve_budget,
)
from apps.ai.tests.factories import AIPromptFactory, make_budget
from apps.assignments.models import Submission
from apps.assignments.tests.factories import SubmissionFactory
from apps.users.models import RoleMembership
from core.permissions import Role
from core.role_principals import RolePrincipal

pytestmark = pytest.mark.django_db


def _seed(tenant):
    with schema_context(tenant.schema_name):
        AIPromptFactory()


def check_and_reserve_budget(**kwargs):
    """Give legacy budget tests a real, exactly owned assignment source."""

    source_id = int(kwargs["source_id"])
    submission = Submission.objects.select_related("student__user").filter(
        pk=source_id
    ).first() or SubmissionFactory(pk=source_id)
    RoleMembership.objects.get_or_create(
        user=submission.student.user,
        branch=submission.assignment.cohort.branch,
        department=None,
        role=Role.STUDENT,
    )
    submission.student.user.refresh_from_db()
    kwargs["requested_by"] = submission.student.user
    kwargs["requested_principal"] = RolePrincipal(
        kind="student",
        principal_id=submission.student_id,
        user_id=submission.student.user_id,
    )
    return _check_and_reserve_budget(**kwargs)


def _record_provider_usage(request: AIRequest, usage: Usage) -> None:
    """Move a reserved request through the durable provider-receipt boundary."""

    request.status = AIRequest.Status.RUNNING
    request.celery_task_id = f"budget-test-{request.pk}"
    request.save(update_fields=["status", "celery_task_id"])
    begin_provider_attempt(ai_request_id=request.pk, task_id=request.celery_task_id)
    record_provider_completion(
        ai_request_id=request.pk,
        usage=usage,
        output="bounded test output",
        provider_request_id=f"provider-budget-test-{request.pk}",
        provider_stop_reason="end_turn",
    )


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


def test_omitted_estimate_reserves_the_selected_prompt_version_cap(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000, monthly_token_limit=100_000)
        req = check_and_reserve_budget(
            feature="assignment_feedback",
            source_app="assignments",
            source_id=82,
        )
        assert req.reserved_tokens == req.prompt.token_cost_cap == 4000


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
        make_budget(
            daily_token_limit=100_000,
            monthly_token_limit=100_000,
            tokens_used_month=99_900,
        )
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
        _record_provider_usage(
            req,
            Usage(
                input_tokens=120,
                output_tokens=80,
                cache_read_tokens=30,
                cache_creation_tokens=20,
            ),
        )
        budget = TenantAIBudget.objects.get(pk=1)
        assert budget.tokens_used_today == 250
        assert budget.tokens_used_month == 250
        req.refresh_from_db()
        assert req.input_tokens == 120
        assert req.output_tokens == 80
        assert req.cache_read_tokens == 30
        assert req.cache_creation_tokens == 20


def test_record_usage_no_double_count_on_terminal(tenant_a):
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000)
        req = check_and_reserve_budget(
            feature="assignment_feedback", estimated_tokens=10, source_app="assignments", source_id=7
        )
        usage = Usage(input_tokens=100, output_tokens=100)
        _record_provider_usage(req, usage)
        req.refresh_from_db()
        # A duplicate provider completion reuses the durable receipt and must
        # not reconcile the same usage twice.
        record_provider_completion(
            ai_request_id=req.pk,
            usage=usage,
            output="different retry output",
            provider_request_id="different-retry-receipt",
            provider_stop_reason="end_turn",
        )
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
            feature="assignment_feedback", estimated_tokens=4_000, source_app="assignments", source_id=8
        )
        budget.refresh_from_db()
        assert budget.tokens_used_today == 4_000  # reset to 0, then 4000 reserved
        assert budget.day_anchor == timezone.localdate()


def test_month_anchor_rolls_over():
    # Travel across a month boundary; the monthly counter resets.
    import apps.tenancy.models  # noqa: F401  (ensure app registry ready)

    with time_machine.travel("2026-02-15", tick=False), schema_context("tenant_a"):
        AIPromptFactory()
        budget = make_budget(daily_token_limit=10_000, monthly_token_limit=10_000)
        budget.tokens_used_month = 9_000
        budget.month_anchor = timezone.localdate()
        budget.save()
    with time_machine.travel("2026-03-01", tick=False), schema_context("tenant_a"):
        check_and_reserve_budget(
            feature="assignment_feedback",
            estimated_tokens=4_000,
            source_app="assignments",
            source_id=9,
        )
        budget = TenantAIBudget.objects.get(pk=1)
        # Reset to 0 on the month rollover, then 5000 reserved (without the reset,
        # 9000+5000 would exceed the 10000 monthly cap and be denied → stay 9000).
        assert budget.tokens_used_month == 4_000


def test_cost_microusd_uses_settings():
    # Every Anthropic receipt class is priced; cache usage must not disappear
    # from cost evidence merely because it is reported outside base input.
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    assert cost_microusd(usage) == 3_000_000 + 15_000_000 + 300_000 + 3_750_000


def test_old_period_failure_cannot_erase_current_period_budget_usage(tenant_a):
    """A delayed job's reservation vanished at rollover; releasing it again must
    not subtract unrelated usage admitted in the new day/month."""
    _seed(tenant_a)
    with schema_context(tenant_a.schema_name):
        make_budget(daily_token_limit=10_000, monthly_token_limit=100_000)
        old_request = check_and_reserve_budget(
            feature="assignment_feedback",
            estimated_tokens=500,
            source_app="assignments",
            source_id=81,
        )
        previous_month_day = timezone.localdate().replace(day=1) - timedelta(days=1)
        AIRequest.objects.filter(pk=old_request.pk).update(
            created_at=timezone.make_aware(datetime.combine(previous_month_day, time.min))
        )
        budget = TenantAIBudget.objects.get(pk=1)
        budget.day_anchor = timezone.localdate()
        budget.month_anchor = timezone.localdate()
        budget.tokens_used_today = 321
        budget.tokens_used_month = 654
        budget.save()

        from apps.ai.services import terminalize_failure

        terminalize_failure(ai_request_id=old_request.pk, error_code="local_failure")

        budget.refresh_from_db()
        assert budget.tokens_used_today == 321
        assert budget.tokens_used_month == 654
