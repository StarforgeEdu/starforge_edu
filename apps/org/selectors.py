"""Branch / Department read selectors + the cached CenterSettings accessor."""

from __future__ import annotations

import logging

from django.core.cache import cache

from core.exceptions import ServiceUnavailableException
from core.utils import current_schema

from .models import Branch, CenterSettings, Department

CENTER_SETTINGS_CACHE_TIMEOUT = 300  # seconds; invalidated on save (receivers.py)
logger = logging.getLogger("starforge.org")


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
    try:
        obj = cache.get(key)
    except Exception:
        # Organization policy is authoritative in Postgres. Redis is only an
        # optimization and must not make every tenant request unavailable.
        logger.warning("Organization settings cache read failed.", exc_info=True)
        obj = None
    if obj is not None and not isinstance(obj, CenterSettings):
        logger.warning("Organization settings cache contained an invalid value.")
        obj = None
    if obj is None:
        # Reads must be observational. Tenant provisioning/migrations create the
        # singleton; a missing row is an operational fault, not permission for a
        # GET or background read to manufacture configuration with guessed defaults.
        obj = CenterSettings.objects.filter(pk=1).first()
        if obj is None:
            raise ServiceUnavailableException(
                "Organization settings are not ready.",
                code="configuration_unavailable",
            )
        try:
            cache.set(key, obj, timeout=CENTER_SETTINGS_CACHE_TIMEOUT)
        except Exception:
            logger.warning("Organization settings cache write failed.", exc_info=True)
    return obj
