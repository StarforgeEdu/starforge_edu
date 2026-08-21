"""Access-config write services (A-2).

``set_override`` / ``clear_override`` are the programmatic upsert/delete API (imported
by other apps' flows + tests); the CRUD service in ``services/v1`` handles the HTTP
endpoints. Both go through standard ORM writes — the override map is read live per
request (no cross-request cache), so a change takes effect on the very next request.
"""

from __future__ import annotations

from django.db import transaction

from apps.access.models import AccountType, AccountTypePermission, RolePermissionOverride
from apps.access.validation import permission_catalogue, validate_effect, validate_permission, validate_role
from core.permissions import ROLE_PERMISSION_MATRIX, _code_allowed, _role_grant_revoke


def set_override(
    *, role: str, permission: str, effect: str, actor=None, note: str = ""
) -> RolePermissionOverride:
    """Create or update the override for (role, permission)."""
    with transaction.atomic():
        role = validate_role(role)
        permission = validate_permission(permission)
        effect = validate_effect(effect)
        obj, _created = RolePermissionOverride.objects.update_or_create(
            role=role,
            permission=permission,
            defaults={"effect": effect, "note": note, "created_by": actor},
        )
        sync_system_account_type(role)
    return obj


def clear_override(*, override: RolePermissionOverride) -> None:
    with transaction.atomic():
        role = override.role
        override.delete()
        sync_system_account_type(role)


def sync_system_account_type(role: str) -> None:
    """Materialize a legacy role's override result into its system AccountType.

    Compatibility routes keep their historical grant/revoke representation, but
    linked memberships authorize from these canonical grant rows. A carve-out
    from ``resource:*`` is expanded into the known concrete verbs so the revoke
    remains effective without adding deny rows to the new model.
    """
    account_type = AccountType.objects.filter(is_system=True, slug=role).first()
    if account_type is None:
        return
    overrides: dict[str, dict[str, str]] = {role: {}}
    for permission, effect in RolePermissionOverride.objects.filter(role=role).values_list(
        "permission", "effect"
    ):
        overrides[role][permission] = effect
    granted, revoked = _role_grant_revoke(role, overrides)
    effective: set[str] = set()
    catalogue = permission_catalogue()
    for permission in granted:
        if permission == "*:*":
            effective.add(permission)
            continue
        resource, _, verb = permission.partition(":")
        if verb == "*" and any(item.partition(":")[0] == resource for item in revoked):
            effective.update(
                code
                for code in catalogue
                if code.partition(":")[0] == resource
                and code.partition(":")[2] != "*"
                and _code_allowed(granted, revoked, code)
            )
        elif _code_allowed(granted, revoked, permission):
            effective.add(permission)

    # Defensive: the seeded row may predate a newly introduced named role.
    if not granted and role in ROLE_PERMISSION_MATRIX:
        effective.update(ROLE_PERMISSION_MATRIX[role])
    AccountTypePermission.objects.filter(account_type=account_type).exclude(permission__in=effective).delete()
    existing = set(
        AccountTypePermission.objects.filter(account_type=account_type).values_list("permission", flat=True)
    )
    AccountTypePermission.objects.bulk_create(
        [
            AccountTypePermission(account_type=account_type, permission=permission)
            for permission in sorted(effective - existing)
        ],
        ignore_conflicts=True,
    )
