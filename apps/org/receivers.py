"""Invalidate the cached CenterSettings on every write (TD-13)."""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.org.models import CenterSettings
from apps.org.selectors import center_settings_cache_key


@receiver(post_save, sender=CenterSettings, dispatch_uid="org.invalidate_center_settings_cache")
def invalidate_center_settings_cache(sender, instance: CenterSettings, **kwargs) -> None:
    settings_key = center_settings_cache_key()
    # Runtime application isolation is durable on CenterSettings and separately
    # cached for the request hot path. Admin/service writes must invalidate both.
    from core.availability import _cache_key as availability_cache_key

    disabled_key = availability_cache_key()
    # Never invalidate/publish cache state for a database write that may still
    # roll back in an enclosing transaction.
    transaction.on_commit(lambda: cache.delete_many((settings_key, disabled_key)))
