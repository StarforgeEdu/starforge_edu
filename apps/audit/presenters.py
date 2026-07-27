"""Audit presenters — plain dict mapper (replaces the DRF serializer)."""

from __future__ import annotations

from typing import Any

from apps.audit.models import AuditLog


def audit_to_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "actor": row.actor_id,
        "actor_username": row.actor.username if row.actor else None,
        "actor_repr": row.actor_repr,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "before": row.before,
        "after": row.after,
        "ip": row.ip,
        "user_agent": row.user_agent,
        "created_at": row.created_at.isoformat(),
    }
