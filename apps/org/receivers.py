"""Invalidate the cached CenterSettings on every write (TD-13)."""

from __future__ import annotations

from django.core.cache import cache
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.org.models import CenterSettings
from apps.org.selectors import center_settings_cache_key


@receiver(post_save, sender=CenterSettings, dispatch_uid="org.invalidate_center_settings_cache")
def invalidate_center_settings_cache(sender, instance: CenterSettings, **kwargs) -> None:
    cache.delete(center_settings_cache_key())
