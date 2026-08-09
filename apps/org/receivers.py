"""Invalidate the cached CenterSettings on every write (TD-13)."""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.org.models import CenterSettings
from apps.org.selectors import (
    _clear_center_settings_dirty,
    _mark_center_settings_dirty,
    center_settings_cache_key,
)

logger = logging.getLogger("starforge.org")


@receiver(post_save, sender=CenterSettings, dispatch_uid="org.invalidate_center_settings_cache")
def invalidate_center_settings_cache(sender, instance: CenterSettings, **kwargs) -> None:
    settings_key = center_settings_cache_key()
    dirty_marker = _mark_center_settings_dirty()
    # Runtime application isolation is durable on CenterSettings and separately
    # cached for the request hot path. Admin/service writes must invalidate both.
    from core.availability import _cache_key as availability_cache_key

    disabled_key = availability_cache_key()

    # Never invalidate/publish cache state for a database write that may still
    # roll back in an enclosing transaction.
    def invalidate_committed_settings() -> None:
        try:
            cache.delete_many((settings_key, disabled_key))
        except Exception:
            # PostgreSQL remains authoritative. A cache outage after commit must
            # not turn a successful settings update into a misleading 500.
            logger.warning("Organization settings cache invalidation failed.", exc_info=True)
        finally:
            _clear_center_settings_dirty(dirty_marker)

    transaction.on_commit(invalidate_committed_settings)
