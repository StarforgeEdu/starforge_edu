"""Tenancy beat tasks. Operate on the public-schema Center table."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from config.celery import app


@app.task
@transaction.atomic
def deactivate_expired_trials() -> int:
    """Deactivate Centers whose trial has lapsed. Idempotent by filter — running
    it twice flips each expired Center exactly once (D1-LB-7)."""
    from apps.tenancy.models import Center, PlatformEvent
    from apps.tenancy.services import record_platform_event

    centers = list(
        Center.objects.select_for_update().filter(
            is_active=True,
            on_trial=True,
            trial_ends_at__lt=timezone.now(),
        )
    )
    for center in centers:
        center.is_active = False
        center.on_trial = False
        center.save(update_fields=["is_active", "on_trial", "updated_at"])
        record_platform_event(
            actor=None,
            center=center,
            event=PlatformEvent.Event.CENTER_TRIAL_EXPIRED,
        )
    return len(centers)
