"""Audit write-side service (TD-9, D3-D-3).

`audit_log()` is the single chokepoint for every audit row — both the model
receivers (`apps.audit.receivers`) and non-model events (auth flows, exports,
billing subscription changes in `schema_context`, D4-E impersonation) call it.
It is imported across the codebase (auth / billing / printing / tenancy / celery),
so it lives here in the domain module (the layered read facade is in `services/v1`).

Masking: any field whose name is in `MASKED_FIELDS` is stored as `"***"` in the
`before`/`after` snapshots. This covers the TD-9 sensitive set:
`national_id`, `medical_notes`, every `ProviderConfig` credential field, and
`password`. Masking is applied centrally here so a new caller cannot forget it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django_tenants.utils import get_public_schema_name

from apps.audit.models import AuditLog
from core.utils import client_ip, current_schema, user_agent

if TYPE_CHECKING:
    from rest_framework.request import Request

# Field names whose values must never be written in plaintext to an audit row.
# Encrypted-at-rest PII (TD-11) + all provider credentials + raw passwords.
MASKED_FIELDS: frozenset[str] = frozenset(
    {
        "national_id",
        "medical_notes",
        "password",
        # ProviderConfig credential fields (apps.payments.models.ProviderConfig)
        "click_secret_key",
        "payme_key",
        "payme_test_key",
        "uzum_api_key",
    }
)

_MASK = "***"


def _on_public_schema() -> bool:
    """True when the active connection is the public/platform schema.

    ``apps.audit`` is TENANT-ONLY, so ``audit_auditlog`` exists only inside tenant
    schemas. Platform-staff writes to the SHARED ``users.User`` /
    ``users.RoleMembership`` tables fire the audit receivers while
    ``connection.schema_name == public`` — without this guard those writes hit a
    non-existent table and raise ProgrammingError, breaking every public-schema
    User/RoleMembership operation (createsuperuser, apex admin, last_login on
    login). Auditing is tenant-scoped, so a public-schema write simply no-ops.
    """
    return current_schema() == get_public_schema_name()


def mask_snapshot(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a copy of `data` with sensitive field values replaced by `"***"`.

    Idempotent and null-safe. Non-dict inputs pass through unchanged so callers
    can hand it `None` (no snapshot) or a value already a primitive.
    """
    if not isinstance(data, dict):
        return data
    return {key: (_MASK if key in MASKED_FIELDS else value) for key, value in data.items()}


def audit_log(
    *,
    actor: Any = None,
    action: str,
    resource_type: str = "",
    resource_id: str | int = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuditLog | None:
    """Append one immutable audit row.

    `actor` may be a `User` instance, an anonymous user, or ``None`` (system).
    `ip`/`user_agent` are extracted from `request` when not passed explicitly.
    `before`/`after` are masked before persistence (see `MASKED_FIELDS`).

    Never raises on a missing/anonymous actor — auditing must not break the
    operation it records. Returns ``None`` (writes nothing) on the public schema,
    where the tenant-only ``audit_auditlog`` table does not exist.
    """
    if _on_public_schema():
        return None
    resolved_ip = ip
    resolved_ua = user_agent
    if request is not None:
        if resolved_ip is None:
            resolved_ip = client_ip(request) or None
        if resolved_ua is None:
            resolved_ua = _user_agent(request)

    return AuditLog.objects.create(
        actor=_actor_instance(actor),
        actor_repr=_actor_repr(actor),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id != "" else "",
        before=mask_snapshot(before),
        after=mask_snapshot(after),
        ip=resolved_ip or None,
        user_agent=(resolved_ua or "")[:512],
    )


def _actor_instance(actor: Any) -> Any:
    """Return a persistable User FK or ``None`` for anonymous/system actors."""
    if actor is None:
        return None
    if not getattr(actor, "is_authenticated", False):
        return None
    if getattr(actor, "pk", None) is None:
        return None
    return actor


def _actor_repr(actor: Any) -> str:
    if actor is None:
        return ""
    if not getattr(actor, "is_authenticated", False):
        return "anonymous"
    return str(actor)[:255]


def _user_agent(request: Request) -> str:
    # Re-export the core helper under a private name so the public kwarg
    # `user_agent` can shadow it in this module without a recursion hazard.
    return user_agent_from_request(request)


# Bound at import time so the kwarg `user_agent` above doesn't shadow the import.
user_agent_from_request = user_agent


def serialize_instance(instance: Any, *, fields: list[str] | None = None) -> dict[str, Any]:
    """JSON-safe field snapshot of a model instance for `before`/`after`.

    Walks concrete local fields, coerces non-JSON types (Decimal, datetime, UUID)
    to strings via `str()`, and stores FK ids as `<name>_id`. The result is then
    masked by `audit_log`; sensitive values never reach JSON.
    """
    snapshot: dict[str, Any] = {}
    for field in instance._meta.concrete_fields:
        if fields is not None and field.name not in fields:
            continue
        if field.is_relation:
            snapshot[field.attname] = getattr(instance, field.attname, None)
            continue
        value = getattr(instance, field.attname, None)
        snapshot[field.name] = _jsonify(value)
    return snapshot


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def diff_snapshots(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce a before/after pair to only the changed keys (for update rows)."""
    if not before or not after:
        return after
    changed = {key: value for key, value in after.items() if before.get(key) != value}
    return changed or None


def audit_log_on_commit(**kwargs: Any) -> None:
    """Schedule an `audit_log` insert for after the surrounding transaction
    commits — used by model receivers so they never record a write that later
    rolls back.

    No-ops on the public schema: ``audit_auditlog`` is tenant-only, so a
    public-schema User/RoleMembership write must not even register the commit
    hook (it would raise ProgrammingError at commit). Checked here at scheduling
    time, when the emitting schema is still active.
    """
    if _on_public_schema():
        return
    transaction.on_commit(lambda: audit_log(**kwargs))
