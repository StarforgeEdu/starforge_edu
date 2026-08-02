"""Shared primitives for immutable historical branch/department ownership."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class ScopeAttributionStatus(models.TextChoices):
    """Confidence/state of an immutable historical scope snapshot."""

    CAPTURED = "captured", _("Captured at write time")
    RESOLVED = "resolved", _("Resolved by reviewed backfill")
    UNRESOLVED = "unresolved", _("Unresolved")
    CONFLICTING = "conflicting", _("Conflicting evidence")
    QUARANTINED = "quarantined", _("Quarantined for review")


ATTRIBUTED_SCOPE_STATUSES = (
    ScopeAttributionStatus.CAPTURED,
    ScopeAttributionStatus.RESOLVED,
)


def guard_immutable_scope_snapshot(
    instance: models.Model,
    *,
    field_attnames: Iterable[str],
    update_fields: Iterable[str] | None,
) -> None:
    """Reject mutation of a persisted historical scope snapshot.

    ``QuerySet.update`` intentionally bypasses model ``save`` and is reserved for
    the reviewed attribution backfill command. Normal domain writes go through
    ``save`` and cannot rewrite branch, department, or attribution state after
    the row is created.
    """
    if instance._state.adding or instance.pk is None:
        return

    attnames = tuple(field_attnames)
    if update_fields is not None:
        updated = {str(field) for field in update_fields}
        tracked_names = {name.removesuffix("_id") for name in attnames}
        if updated.isdisjoint(attnames) and updated.isdisjoint(tracked_names):
            return

    previous: dict[str, Any] | None = (
        type(instance)._default_manager.filter(pk=instance.pk).values(*attnames).first()
    )
    if previous is None:
        return
    changed = [name.removesuffix("_id") for name in attnames if previous[name] != getattr(instance, name)]
    if changed:
        raise ValidationError(
            {field: [str(_("Historical scope attribution is immutable."))] for field in changed}
        )
