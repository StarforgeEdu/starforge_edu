"""Audit write-side service (TD-9, D3-D-3).

`audit_log()` is the single chokepoint for every audit row — both the model
receivers (`apps.audit.receivers`) and non-model events (auth flows, exports,
billing subscription changes in `schema_context`, D4-E impersonation) call it.
It is imported across the codebase (auth / billing / printing / tenancy / celery),
so it lives here in the domain module (the layered read facade is in `services/v1`).

Masking: sensitive field values are stored as `"***"` in `before`/`after`.
This covers encrypted identity, medical, family-safeguarding, emergency-contact,
and provider-credential fields. Masking is applied centrally so callers cannot
accidentally copy decrypted values into the append-only audit trail.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db import transaction

from apps.audit.models import AuditLog
from apps.audit.privacy import privacy_safe_audit_snapshot
from apps.audit.scopes import AuditScopeSnapshot, infer_audit_scope
from core.role_principals import PRINCIPAL_KINDS, RolePrincipal
from core.utils import client_ip, user_agent

if TYPE_CHECKING:
    from django.http import HttpRequest
    from rest_framework.request import Request

    type AuditRequest = HttpRequest | Request

# Field names whose values must never be written in plaintext to an audit row.
# Encrypted-at-rest PII (TD-11) + all provider credentials + raw passwords.
MASKED_FIELDS: frozenset[str] = frozenset(
    {
        "national_id",
        "medical_notes",
        "emergency_contacts",
        "custody_notes",
        "password",
        # ProviderConfig credential fields (apps.payments.models.ProviderConfig)
        "click_secret_key",
        "payme_key",
        "payme_test_key",
        "uzum_api_key",
    }
)

# ``notes`` is too generic to mask globally (many operational models use it for
# non-sensitive text), but ParentProfile.notes is a safeguarding field.
_RESOURCE_MASKED_FIELDS: dict[str, frozenset[str]] = {
    "parents.ParentProfile": frozenset({"notes"}),
    # Organization-directory readers do not automatically hold finance access.
    # Preserve evidence that a department changed without turning the audit
    # feed into a historical budget oracle.
    "org.Department": frozenset({"budget"}),
    "org.CenterSettings": frozenset(
        {
            "fx_rate_usd_manual",
            "otp_channel_prefs",
        }
    ),
    "approvals.ApprovalRequest": frozenset(
        {
            "idempotency_key_hash",
            "operation_fingerprint",
            "domain_dedupe_key",
        }
    ),
    "payroll.PayrollPeriod": frozenset(
        {
            "run_idempotency_key_hash",
            "run_fingerprint",
        }
    ),
    "payroll.PayrollAdjustment": frozenset(
        {
            "idempotency_key_hash",
            "operation_fingerprint",
        }
    ),
    "payroll.PayrollReconciliation": frozenset(
        {
            "idempotency_key_hash",
            "operation_fingerprint",
            "external_reference",
        }
    ),
    "payroll.PayrollExport": frozenset(
        {
            "idempotency_key_hash",
            "operation_fingerprint",
            "s3_key",
        }
    ),
}

_MASK = "***"
_AUDIT_PRINCIPAL_KINDS = PRINCIPAL_KINDS | {"user"}


def audit_sensitivity(
    *,
    resource_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> str:
    """Classify sensitive history at write time without a resource lookup."""
    if resource_type.startswith("payroll."):
        return AuditLog.Sensitivity.COMPENSATION
    if resource_type in {"teachers.TeacherProfile", "teachers.PayoutPolicy"}:
        return AuditLog.Sensitivity.COMPENSATION
    if resource_type == "approvals.ApprovalRequest" and any(
        isinstance(snapshot, dict) and snapshot.get("kind") == "salary_prep" for snapshot in (before, after)
    ):
        return AuditLog.Sensitivity.COMPENSATION
    if resource_type == "approvals.LedgerEntry" and any(
        isinstance(snapshot, dict) and snapshot.get("entry_type") == "salary_prep"
        for snapshot in (before, after)
    ):
        return AuditLog.Sensitivity.COMPENSATION
    return AuditLog.Sensitivity.STANDARD


def mask_snapshot(
    data: dict[str, Any] | None,
    *,
    resource_type: str = "",
) -> dict[str, Any] | None:
    """Return a copy of `data` with sensitive field values replaced by `"***"`.

    Idempotent and null-safe. Non-dict inputs pass through unchanged so callers
    can hand it `None` (no snapshot) or a value already a primitive.
    """
    if not isinstance(data, dict):
        return data
    masked = MASKED_FIELDS | _RESOURCE_MASKED_FIELDS.get(resource_type, frozenset())
    return {key: (_MASK if key in masked else value) for key, value in data.items()}


def audit_log(
    *,
    actor: Any = None,
    action: str,
    resource_type: str = "",
    resource_id: str | int = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: AuditRequest | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    scope: AuditScopeSnapshot | None = None,
    actor_principal: RolePrincipal | None = None,
) -> AuditLog:
    """Append one immutable audit row.

    `actor` may be a `User` instance, an anonymous user, or ``None`` (system).
    `ip`/`user_agent` are extracted from `request` when not passed explicitly.
    `before`/`after` are masked before persistence (see `MASKED_FIELDS`).  A
    caller should pass a write-time ``scope`` whenever the operation owns an
    explicit branch/department boundary.  The narrow inference fallback uses
    only the supplied immutable snapshots and otherwise marks the event
    unresolved; it never joins the resource's current placement.

    Missing/anonymous actors are recorded as system events. AuditLog exists in
    both public and tenant schemas so platform mutations are never discarded.
    """
    row = _build_audit_log(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
        request=request,
        ip=ip,
        user_agent=user_agent,
        scope=scope,
        actor_principal=actor_principal,
    )
    row.save(force_insert=True)
    return row


def _build_audit_log(
    *,
    actor: Any = None,
    action: str,
    resource_type: str = "",
    resource_id: str | int = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: AuditRequest | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    scope: AuditScopeSnapshot | None = None,
    actor_principal: RolePrincipal | None = None,
) -> AuditLog:
    """Build one unsaved, centrally masked audit row."""
    try:
        normalized_action = AuditLog.Action(action).value
    except (TypeError, ValueError) as exc:
        raise ValueError("action must be a documented audit action") from exc
    attribution_request = request
    if attribution_request is None:
        # Domain services often receive the actor but not the HttpRequest. The
        # middleware-bound object is server-owned request context, so it can
        # safely supply the already validated role identity without plumbing a
        # bridge User through every service interface. Background work has no
        # request context and therefore remains system/unresolved as designed.
        from apps.audit.context import current_request

        attribution_request = current_request()
    resolved_ip = ip
    resolved_ua = user_agent
    if request is not None:
        if resolved_ip is None:
            resolved_ip = client_ip(request) or None
        if resolved_ua is None:
            resolved_ua = _user_agent(request)
    resolved_scope = scope or infer_audit_scope(
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
    )
    if not isinstance(resolved_scope, AuditScopeSnapshot):
        raise TypeError("scope must be an AuditScopeSnapshot")
    safe_before = privacy_safe_audit_snapshot(
        action=normalized_action,
        resource_type=resource_type,
        snapshot=before,
    )
    safe_after = privacy_safe_audit_snapshot(
        action=normalized_action,
        resource_type=resource_type,
        snapshot=after,
    )
    persisted_actor = _actor_instance(actor)
    actor_status, actor_kind, actor_id = _actor_attribution(
        actor=persisted_actor,
        request=attribution_request,
        actor_principal=actor_principal,
    )
    return AuditLog(
        actor=persisted_actor,
        actor_repr=_actor_repr(actor),
        actor_attribution_status=actor_status,
        actor_principal_kind=actor_kind,
        actor_principal_id=actor_id,
        action=normalized_action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id != "" else "",
        before=mask_snapshot(safe_before, resource_type=resource_type),
        after=mask_snapshot(safe_after, resource_type=resource_type),
        ip=resolved_ip or None,
        user_agent=(resolved_ua or "")[:512],
        scope_status=resolved_scope.status,
        scope_branch_id=resolved_scope.branch_id,
        scope_department_id=resolved_scope.department_id,
        sensitivity=audit_sensitivity(
            resource_type=resource_type,
            before=before,
            after=after,
        ),
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


def _actor_attribution(
    *,
    actor: Any,
    request: AuditRequest | None,
    actor_principal: RolePrincipal | None,
) -> tuple[str, str, int | None]:
    """Return a fail-closed immutable actor snapshot.

    Existing rows and actor-only calls remain unresolved because a bridge User
    may back several role accounts. A server-validated request or an explicit
    internal RolePrincipal is exact. Missing/anonymous actors are system events.
    """

    if actor_principal is not None:
        if not isinstance(actor_principal, RolePrincipal):
            raise TypeError("actor_principal must be a RolePrincipal")
        if (
            actor is None
            or actor_principal.kind not in _AUDIT_PRINCIPAL_KINDS
            or isinstance(actor_principal.principal_id, bool)
            or not isinstance(actor_principal.principal_id, int)
            or actor_principal.principal_id <= 0
            or actor_principal.user_id != actor.pk
        ):
            raise ValueError("actor_principal must identify the persisted audit actor")
        return (
            AuditLog.ActorAttributionStatus.EXACT,
            actor_principal.kind,
            actor_principal.principal_id,
        )

    if actor is not None and request is not None:
        request_actor = _actor_instance(getattr(request, "user", None))
        from core.session_auth import session_validated_request_principal

        if (
            request_actor is not None
            and request_actor.pk == actor.pk
            and session_validated_request_principal(request)
        ):
            kind = str(getattr(request, "principal_kind", "") or "")
            principal_id = getattr(request, "principal_id", None)
            if (
                kind in PRINCIPAL_KINDS
                and isinstance(principal_id, int)
                and not isinstance(principal_id, bool)
                and principal_id > 0
            ):
                return AuditLog.ActorAttributionStatus.EXACT, kind, principal_id

            # A blank validated session is exact only on the public control
            # plane. Tenant production sessions must always be role-native.
            from django_tenants.utils import get_public_schema_name

            from core.utils import current_schema

            if not kind and principal_id is None and current_schema() == get_public_schema_name():
                return AuditLog.ActorAttributionStatus.EXACT, "user", actor.pk

    if actor is None:
        return AuditLog.ActorAttributionStatus.SYSTEM, "", None
    return AuditLog.ActorAttributionStatus.UNRESOLVED, "", None


def _user_agent(request: AuditRequest) -> str:
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

    The callback is registered in public and tenant schemas and only runs after
    the surrounding mutation commits.
    """
    transaction.on_commit(lambda: audit_log(**kwargs))


def audit_logs_bulk_on_commit(entries: list[dict[str, Any]]) -> None:
    """Insert many audit events in one query after their mutation commits."""
    frozen_entries = [dict(entry) for entry in entries]

    def write() -> None:
        if frozen_entries:
            AuditLog.objects.bulk_create([_build_audit_log(**entry) for entry in frozen_entries])

    transaction.on_commit(write)
