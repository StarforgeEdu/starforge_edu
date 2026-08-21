"""Audit presenters — plain dict mapper (replaces the DRF serializer)."""

from __future__ import annotations

from typing import Any

from apps.audit.models import AuditLog
from apps.audit.privacy import privacy_safe_audit_snapshot


def audit_to_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "actor": row.actor_id,
        "actor_username": row.actor.username if row.actor else None,
        "actor_repr": row.actor_repr,
        "actor_principal": {
            "status": row.actor_attribution_status,
            "kind": row.actor_principal_kind or None,
            "id": row.actor_principal_id,
        },
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "before": privacy_safe_audit_snapshot(
            action=row.action,
            resource_type=row.resource_type,
            snapshot=row.before,
        ),
        "after": privacy_safe_audit_snapshot(
            action=row.action,
            resource_type=row.resource_type,
            snapshot=row.after,
        ),
        "ip": row.ip,
        "user_agent": row.user_agent,
        "scope": {
            "status": row.scope_status,
            "branch": row.scope_branch_id,
            "department": row.scope_department_id,
        },
        "sensitivity": row.sensitivity,
        "created_at": row.created_at.isoformat(),
    }
