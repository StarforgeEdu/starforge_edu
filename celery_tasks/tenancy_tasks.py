"""Tenancy beat tasks. Operate on the public-schema Center table."""

from __future__ import annotations

from django.utils import timezone

from config.celery import app


@app.task
def deactivate_expired_trials() -> int:
    """Deactivate Centers whose trial has lapsed. Idempotent by filter — running
    it twice flips each expired Center exactly once (D1-LB-7)."""
    from apps.tenancy.models import Center

    return Center.objects.filter(is_active=True, on_trial=True, trial_ends_at__lt=timezone.now()).update(
        is_active=False
    )
