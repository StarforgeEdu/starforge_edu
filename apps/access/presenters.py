"""Access-config presenters — plain dict mapper (replaces the DRF serializer)."""

from __future__ import annotations

from typing import Any, cast

from apps.access.models import AccountType, RolePermissionOverride
from apps.users.models import RoleMembership


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


def account_type_to_dict(account_type: AccountType) -> dict[str, Any]:
    return {
        "id": account_type.pk,
        "name": account_type.name,
        "slug": account_type.slug,
        "account_kind": account_type.account_kind,
        "description": account_type.description,
        "is_active": account_type.is_active,
        "is_system": account_type.is_system,
        "is_owner_type": account_type.is_owner_type,
        "permissions": sorted(permission.permission for permission in account_type.permission_rows.all()),
        "created_at": account_type.created_at.isoformat(),
        "updated_at": account_type.updated_at.isoformat(),
    }


def account_type_assignment_to_dict(membership: RoleMembership) -> dict[str, Any]:
    from apps.access.services.account_types import assignment_principal_identity

    principal_kind, principal_id = assignment_principal_identity(membership)
    account_type = cast(AccountType, membership.account_type)
    department = membership.department if membership.department_id else None
    return {
        "id": membership.pk,
        "account_type": membership.account_type_id,
        "account_type_name": account_type.name,
        "principal_kind": principal_kind,
        "principal_id": principal_id,
        "principal_missing": principal_id is None,
        "branch": membership.branch_id,
        "branch_name": membership.branch.name if membership.branch_id else None,
        "department": membership.department_id,
        "department_name": department.name if department is not None else None,
        "granted_at": membership.granted_at.isoformat(),
        "granted_by": membership.granted_by_id,
    }
