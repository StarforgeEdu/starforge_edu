"""AI read-side selectors (D4-LA-9).

``tokens_consumed(start, end)`` is the **ai-tokens-consumed** interface
published to WORKLOG: Lane B's ``ai_usage`` report generator, Lane B's nightly
aggregation, and Day-3 billing metering all consume it. It runs inside the
active tenant schema and sums input+output tokens of every ``AIRequest`` created
in the window.
"""

from __future__ import annotations

from datetime import date, datetime, time

from django.db.models import BigIntegerField, F, IntegerField, QuerySet, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.ai.models import AIRequest


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Inclusive ``[start, end]`` calendar days -> tz-aware datetime range
    ``[start 00:00, (end+1) 00:00)`` in the active timezone."""
    from datetime import timedelta

    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(start, time.min), tz)
    hi = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    return lo, hi


def tokens_consumed(start: date, end: date) -> int:
    """Total AI tokens (input + output) consumed in the inclusive day range.

    Counts every ``AIRequest`` created in the window regardless of status —
    denied/failed rows carry 0 tokens, so they contribute nothing but remain
    correct if a future feature reserves tokens up front.
    """
    lo, hi = _day_bounds(start, end)
    agg = AIRequest.objects.filter(created_at__gte=lo, created_at__lt=hi).aggregate(
        total=Coalesce(Sum(F("input_tokens") + F("output_tokens")), Value(0), output_field=IntegerField())
    )
    return int(agg["total"] or 0)


def tokens_used_current_month() -> int:
    """AI tokens consumed by the current tenant this calendar month.

    Kept for Day-3 billing metering (``celery_tasks/billing_tasks._ai_tokens``)
    which imports this lazily. Delegates to ``tokens_consumed`` for the current
    month so there is a single accounting path (no drift between billing and the
    usage report).
    """
    today = timezone.localdate()
    return tokens_consumed(today.replace(day=1), today)


def cost_consumed(start: date, end: date) -> int:
    """Total AI provider cost (micro-USD) of every ``AIRequest`` created in the inclusive
    day range — the platform's underlying cost, summed for metered overage billing (F9-2).
    Mirrors ``tokens_consumed`` so billing and the usage report share one accounting path."""
    lo, hi = _day_bounds(start, end)
    agg = AIRequest.objects.filter(created_at__gte=lo, created_at__lt=hi).aggregate(
        total=Coalesce(Sum("cost_microusd"), Value(0), output_field=BigIntegerField())
    )
    return int(agg["total"] or 0)


def cost_used_current_month() -> int:
    """AI provider cost (micro-USD) consumed by the current tenant this calendar month
    (F9-2 metered billing). Delegates to ``cost_consumed`` for a single accounting path."""
    today = timezone.localdate()
    return cost_consumed(today.replace(day=1), today)


def list_requests() -> QuerySet[AIRequest]:
    """All AI requests in the tenant, newest first, with the prompt eager-loaded
    (the read serializer reports the prompt feature/version)."""
    return AIRequest.objects.select_related("prompt", "requested_by").all()


def usage_report(*, start: date, end: date) -> list[dict]:
    """Per-feature usage totals for the inclusive day range (D4-LA-8 report)."""
    from django.db.models import Count

    lo, hi = _day_bounds(start, end)
    rows = (
        AIRequest.objects.filter(created_at__gte=lo, created_at__lt=hi)
        .values("feature")
        .annotate(
            requests=Count("id"),
            input_tokens=Coalesce(Sum("input_tokens"), Value(0)),
            output_tokens=Coalesce(Sum("output_tokens"), Value(0)),
            cost_microusd=Coalesce(Sum("cost_microusd"), Value(0)),
        )
        .order_by("feature")
    )
    return [
        {
            "feature": r["feature"],
            "requests": int(r["requests"]),
            "input_tokens": int(r["input_tokens"]),
            "output_tokens": int(r["output_tokens"]),
            "cost_microusd": int(r["cost_microusd"]),
        }
        for r in rows
    ]
