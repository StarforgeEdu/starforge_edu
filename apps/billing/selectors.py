"""Billing read-side selectors (PUBLIC schema).

All reads here run on the public schema (Plan/Subscription/UsageSnapshot live
only there). The middleware's per-request subscription lookup is cached for 60s
in Redis to avoid a public-schema query on every tenant request.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, timedelta

from django.core.cache import cache
from django.db.models import QuerySet
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.billing.models import AiUsageCharge, Plan, Subscription, UsageSnapshot

SUBSCRIPTION_CACHE_TIMEOUT = 60  # seconds (D3-E-4: avoid a public-schema query per request)


def subscription_cache_key(schema_name: str) -> str:
    return f"billing:subscription_status:{schema_name}"


def get_subscription_status(*, schema_name: str, center_id: int) -> str | None:
    """Cached subscription status for the tenant identified by `schema_name`.

    Returns the status string, or ``None`` if the Center has no subscription
    row yet (treated as pass-through by the gate — provisioning auto-creates one).
    The cache is invalidated by `services._invalidate_subscription_cache` on any
    status write.

    Billing tables live ONLY in the public schema (apps.billing is in
    SHARED_APPS). This is called from the gate middleware while the TENANT
    schema is active, so the query MUST run inside a public schema_context —
    otherwise Postgres' search_path has no billing_subscription relation. On a
    cache hit (the common path) no query runs and the context switch is skipped.
    """
    key = subscription_cache_key(schema_name)
    # Degrade gracefully if the cache (Redis) is unavailable: fall back to a direct
    # public-schema read instead of letting the gate middleware 500 every tenant
    # request on a Redis outage.
    try:
        cached = cache.get(key)
    except Exception:  # any cache backend error -> treat as a miss, read from DB
        cached = None
    if cached is not None:
        # Sentinel "" means "no subscription row" (distinct from a cache miss).
        return cached or None
    with schema_context(get_public_schema_name()):
        status = Subscription.objects.filter(center_id=center_id).values_list("status", flat=True).first()
    with contextlib.suppress(Exception):  # a cache write failure must not break the request
        cache.set(key, status or "", timeout=SUBSCRIPTION_CACHE_TIMEOUT)
    return status


def active_plans() -> QuerySet[Plan]:
    return Plan.objects.filter(is_active=True)


def subscription_for_center(*, center_id: int) -> Subscription | None:
    return Subscription.objects.select_related("plan", "center").filter(center_id=center_id).first()


def usage_for_center(*, center_id: int) -> QuerySet[UsageSnapshot]:
    return UsageSnapshot.objects.filter(center_id=center_id).select_related("center")


def ai_charges_for_center(*, center_id: int) -> QuerySet[AiUsageCharge]:
    """F9-2: a Center's metered AI-overage charges, newest billing month first."""
    return AiUsageCharge.objects.filter(center_id=center_id).select_related("center")


def center_dau(*, schema_name: str, on: date | None = None) -> int:
    """Daily active users for a tenant on `on` (default today, tenant TZ).

    Counts `users.User` rows whose `last_seen_at` falls on the given date,
    computed INSIDE the tenant schema (users.User is per-schema under TD-3).
    Used for the live `today` point on the platform usage endpoint (D4-LE-2);
    the nightly aggregation (D4-LB-7) persists historical DAU into snapshots.
    """
    on = on or timezone.localdate()
    start = timezone.make_aware(datetime(on.year, on.month, on.day))
    end = start + timedelta(days=1)
    with schema_context(schema_name):
        from apps.users.models import User

        return User.objects.filter(last_seen_at__gte=start, last_seen_at__lt=end).count()


def usage_series(*, center, days: int = 30) -> dict:
    """Per-center usage payload for the control center (D4-LE-2).

    Returns ``{"series": [...], "today": {...}}`` where `series` is the last
    `days` nightly `UsageSnapshot` rows (Lane B writes them) and `today` is a
    LIVE point: live DAU from `users.User.last_seen_at` + the latest snapshot's
    students/storage/AI figures (so the dashboard isn't blank before the
    nightly run). Each point carries its `dau` from the snapshot's stored value;
    snapshots persist DAU via the nightly job (D4-LB-7 field per WORKLOG).
    """
    today = timezone.localdate()
    floor = today - timedelta(days=max(days, 1) - 1)
    rows = list(
        UsageSnapshot.objects.filter(center=center, date__gte=floor, date__lte=today).order_by("date")
    )
    series = [_snapshot_point(r) for r in rows]

    latest = rows[-1] if rows else None
    today_point = {
        "date": today,
        "dau": center_dau(schema_name=center.schema_name, on=today),
        "students": latest.students_count if latest else 0,
        "storage_bytes": latest.storage_bytes if latest else 0,
        "ai_tokens": latest.ai_tokens_used if latest else 0,
    }
    return {"series": series, "today": today_point}


def _snapshot_point(row: UsageSnapshot) -> dict:
    # `dau` is read from the snapshot if Lane B's nightly job stores it; absent
    # the column it degrades to 0 (the live `today` point always has real DAU).
    return {
        "date": row.date,
        "dau": getattr(row, "dau", 0) or 0,
        "students": row.students_count,
        "storage_bytes": row.storage_bytes,
        "ai_tokens": row.ai_tokens_used,
    }
