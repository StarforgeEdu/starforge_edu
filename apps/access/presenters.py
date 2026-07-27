"""Access-config presenters — plain dict mapper (replaces the DRF serializer)."""

from __future__ import annotations

from typing import Any

from apps.access.models import RolePermissionOverride


def override_to_dict(o: RolePermissionOverride) -> dict[str, Any]:
    return {
        "id": o.id,
        "role": o.role,
        "permission": o.permission,
        "effect": o.effect,
        "note": o.note,
        "created_by": o.created_by_id,
        "created_at": o.created_at.isoformat(),
        "updated_at": o.updated_at.isoformat(),
    }
