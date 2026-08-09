"""Branch / Department read selectors + the cached CenterSettings accessor."""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.db import connection

from core.exceptions import ServiceUnavailableException
from core.utils import current_schema

from .models import Branch, CenterSettings, Department

CENTER_SETTINGS_CACHE_TIMEOUT = 300  # seconds; invalidated on save (receivers.py)
logger = logging.getLogger("starforge.org")
_DIRTY_SETTINGS_ATTR = "_starforge_dirty_center_settings"


def list_branches():
    return Branch.objects.filter(is_active=True)


def list_departments_in_branch(branch_id: int):
    return Department.objects.filter(branch_id=branch_id, is_active=True)


def center_settings_cache_key() -> str:
    return f"center_settings:{current_schema()}"


def _mark_center_settings_dirty() -> tuple[str, object] | None:
    """Mark this schema as changed inside the current outer transaction.

    Shared cache publication must wait for commit, but reads on the same database
    transaction still need write-your-writes semantics. The marker makes those
    reads bypass (and not populate) the shared cache. Tying it to the outer Atomic
    object lets a later transaction discard a marker whose rollback callback never
    ran, without relying on test-only transaction behavior.
    """

    if not connection.in_atomic_block or not connection.atomic_blocks:
        return None
    schema = current_schema()
    transaction_marker = connection.atomic_blocks[0]
    dirty = dict(getattr(connection, _DIRTY_SETTINGS_ATTR, {}))
    dirty[schema] = transaction_marker
    setattr(connection, _DIRTY_SETTINGS_ATTR, dirty)
    return schema, transaction_marker


def _clear_center_settings_dirty(marker: tuple[str, object] | None) -> None:
    if marker is None:
        return
    schema, transaction_marker = marker
    dirty = dict(getattr(connection, _DIRTY_SETTINGS_ATTR, {}))
    if dirty.get(schema) is transaction_marker:
        dirty.pop(schema, None)
    setattr(connection, _DIRTY_SETTINGS_ATTR, dirty)


def _center_settings_dirty_in_current_transaction() -> bool:
    schema = current_schema()
    dirty = dict(getattr(connection, _DIRTY_SETTINGS_ATTR, {}))
    marker = dirty.get(schema)
    if marker is None:
        return False
    if any(block is marker for block in connection.atomic_blocks):
        return True
    # A rollback discards on_commit callbacks. Lazily remove its orphan marker
    # when this connection is next used in a different transaction.
    dirty.pop(schema, None)
    setattr(connection, _DIRTY_SETTINGS_ATTR, dirty)
    return False


def get_center_settings() -> CenterSettings:
    """TD-13 accessor: the per-Center singleton, cached per tenant schema.

    Never stale for more than one save — `apps.org.receivers` deletes the key
    on every CenterSettings write.
    """
    key = center_settings_cache_key()
    transaction_dirty = _center_settings_dirty_in_current_transaction()
    if transaction_dirty:
        obj = None
    else:
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
        if not transaction_dirty:
            try:
                cache.set(key, obj, timeout=CENTER_SETTINGS_CACHE_TIMEOUT)
            except Exception:
                logger.warning("Organization settings cache write failed.", exc_info=True)
    return obj
