"""Access-config write services (A-2).

``set_override`` / ``clear_override`` are the programmatic upsert/delete API (imported
by other apps' flows + tests); the CRUD service in ``services/v1`` handles the HTTP
endpoints. Both go through standard ORM writes — the override map is read live per
request (no cross-request cache), so a change takes effect on the very next request.
"""

from __future__ import annotations

from apps.access.models import RolePermissionOverride
from apps.access.validation import validate_effect, validate_permission, validate_role


def set_override(
    *, role: str, permission: str, effect: str, actor=None, note: str = ""
) -> RolePermissionOverride:
    """Create or update the override for (role, permission)."""
    role = validate_role(role)
    permission = validate_permission(permission)
    effect = validate_effect(effect)
    obj, _created = RolePermissionOverride.objects.update_or_create(
        role=role,
        permission=permission,
        defaults={"effect": effect, "note": note, "created_by": actor},
    )
    return obj


def clear_override(*, override: RolePermissionOverride) -> None:
    override.delete()
