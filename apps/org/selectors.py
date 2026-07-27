"""Branch / Department read selectors + the cached CenterSettings accessor."""

from __future__ import annotations

from django.core.cache import cache

from core.utils import current_schema

from .models import Branch, CenterSettings, Department

CENTER_SETTINGS_CACHE_TIMEOUT = 300  # seconds; invalidated on save (receivers.py)


def list_branches():
    return Branch.objects.filter(is_active=True)


def list_departments_in_branch(branch_id: int):
    return Department.objects.filter(branch_id=branch_id, is_active=True)


def center_settings_cache_key() -> str:
    return f"center_settings:{current_schema()}"


def get_center_settings() -> CenterSettings:
    """TD-13 accessor: the per-Center singleton, cached per tenant schema.

    Never stale for more than one save — `apps.org.receivers` deletes the key
    on every CenterSettings write.
    """
    key = center_settings_cache_key()
    obj = cache.get(key)
    if obj is None:
        obj = CenterSettings.load()
        cache.set(key, obj, timeout=CENTER_SETTINGS_CACHE_TIMEOUT)
    return obj
