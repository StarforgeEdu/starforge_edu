"""Canonical account-type management and role-profile assignment services."""

from __future__ import annotations

from typing import Any, cast

from django.apps import apps as django_apps
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.models import AccountType, AccountTypePermission, RolePermissionOverride
from apps.access.validation import (
    validate_account_kind,
    validate_account_type_name,
    validate_account_type_permission,
    validate_account_type_slug,
)
from apps.audit.services import audit_log
from apps.org.models import Branch, Department
from apps.users.models import RoleMembership
from core.exceptions import ConflictException, NotFoundException, ValidationException
from core.permissions import Role, _code_allowed, _role_grant_revoke
from core.scoping import (
    assert_permission_membership_scope,
    assert_permission_organization_scope,
    scope_to_permission_memberships,
)

_PRINCIPAL_MODELS: dict[str, tuple[str, str]] = {
    AccountType.AccountKind.STAFF: ("org", "StaffProfile"),
    AccountType.AccountKind.TEACHER: ("teachers", "TeacherProfile"),
    AccountType.AccountKind.STUDENT: ("students", "StudentProfile"),
    AccountType.AccountKind.PARENT: ("parents", "ParentProfile"),
}

_PRINCIPAL_RELATIONS: dict[str, str] = {
    AccountType.AccountKind.STAFF: "staff_profile",
    AccountType.AccountKind.TEACHER: "teacher_profile",
    AccountType.AccountKind.STUDENT: "student_profile",
    AccountType.AccountKind.PARENT: "parent_profile",
}

_LEGACY_ROLES_BY_PRINCIPAL_KIND: dict[str, frozenset[str]] = {
    AccountType.AccountKind.STUDENT: frozenset({Role.STUDENT}),
    AccountType.AccountKind.TEACHER: frozenset({Role.TEACHER}),
    AccountType.AccountKind.PARENT: frozenset({Role.PARENT}),
    AccountType.AccountKind.STAFF: frozenset(set(Role.ALL) - {Role.STUDENT, Role.TEACHER, Role.PARENT}),
}


def account_type_queryset() -> QuerySet[AccountType]:
    return AccountType.objects.prefetch_related("permission_rows").all()


def assignment_queryset(
    request: Any,
    *,
    permission: str = "access:read",
) -> QuerySet[RoleMembership]:
    queryset = (
        RoleMembership.objects.filter(
            revoked_at__isnull=True,
            account_type__isnull=False,
        )
        .select_related(
            "account_type",
            "branch",
            "department",
            "user__staff_profile",
            "user__teacher_profile",
            "user__student_profile",
            "user__parent_profile",
        )
        .order_by("account_type__name", "branch__name", "pk")
    )
    return scope_to_permission_memberships(
        request,
        queryset,
        permission=permission,
        branch_field="branch_id",
        department_field="department_id",
        account_kinds={AccountType.AccountKind.STAFF},
    )


def get_account_type(pk: int, *, for_update: bool = False) -> AccountType:
    queryset = account_type_queryset()
    if for_update:
        queryset = queryset.select_for_update()
    account_type = queryset.filter(pk=pk).first()
    if account_type is None:
        raise NotFoundException(_("Account type not found."), code="account_type_not_found")
    return account_type


@transaction.atomic
def create_account_type(
    *,
    name: str,
    slug: str,
    account_kind: str,
    description: str,
    is_active: bool,
    permissions: list[str],
    actor: Any,
    request: Any,
) -> AccountType:
    assert_permission_organization_scope(
        request,
        permission="access:write",
        account_kinds={AccountType.AccountKind.STAFF},
    )
    name = validate_account_type_name(name)
    slug = validate_account_type_slug(slug)
    account_kind = validate_account_kind(account_kind)
    _validate_type_uniqueness(name=name, slug=slug)
    account_type = AccountType.objects.create(
        name=name,
        slug=slug,
        account_kind=account_kind,
        description=description.strip(),
        is_active=is_active,
        is_system=False,
    )
    _replace_permissions(account_type, permissions)
    audit_log(
        actor=actor,
        action="create",
        resource_type="access.account_type",
        resource_id=account_type.pk,
        after=_type_snapshot(account_type),
        request=request,
    )
    return account_type_queryset().get(pk=account_type.pk)


@transaction.atomic
def update_account_type(
    account_type: AccountType,
    changes: dict[str, Any],
    *,
    actor: Any,
    request: Any,
) -> AccountType:
    assert_permission_organization_scope(
        request,
        permission="access:write",
        account_kinds={AccountType.AccountKind.STAFF},
    )
    account_type = get_account_type(account_type.pk, for_update=True)
    if account_type.is_owner_type:
        raise ConflictException(
            _("The protected system owner type cannot be changed."),
            code="protected_account_type",
        )
    before = _type_snapshot(account_type)
    if account_type.is_system and any(key in changes for key in ("name", "slug", "account_kind")):
        raise ConflictException(
            _("System account type identity cannot be changed."),
            code="protected_account_type",
        )
    name = validate_account_type_name(changes.get("name", account_type.name))
    slug = validate_account_type_slug(changes.get("slug", account_type.slug))
    account_kind = validate_account_kind(changes.get("account_kind", account_type.account_kind))
    if account_kind != account_type.account_kind and account_type.memberships.exists():
        raise ConflictException(
            _("An assigned account type cannot change account kind."),
            code="account_type_assigned",
        )
    _validate_type_uniqueness(name=name, slug=slug, exclude_pk=account_type.pk)
    account_type.name = name
    account_type.slug = slug
    account_type.account_kind = account_kind
    if "description" in changes:
        account_type.description = str(changes["description"]).strip()
    if "is_active" in changes:
        account_type.is_active = bool(changes["is_active"])
    account_type.save()
    audit_log(
        actor=actor,
        action="update",
        resource_type="access.account_type",
        resource_id=account_type.pk,
        before=before,
        after=_type_snapshot(account_type),
        request=request,
    )
    return account_type_queryset().get(pk=account_type.pk)


@transaction.atomic
def delete_account_type(account_type: AccountType, *, actor: Any, request: Any) -> None:
    assert_permission_organization_scope(
        request,
        permission="access:write",
        account_kinds={AccountType.AccountKind.STAFF},
    )
    account_type = get_account_type(account_type.pk, for_update=True)
    if account_type.is_system:
        raise ConflictException(
            _("System account types cannot be deleted."),
            code="protected_account_type",
        )
    if account_type.memberships.exists():
        raise ConflictException(
            _("Revoke every assignment before deleting this account type."),
            code="account_type_assigned",
        )
    before = _type_snapshot(account_type)
    account_type_id = account_type.pk
    account_type.delete()
    audit_log(
        actor=actor,
        action="delete",
        resource_type="access.account_type",
        resource_id=account_type_id,
        before=before,
        request=request,
    )


@transaction.atomic
def replace_account_type_permissions(
    account_type: AccountType,
    permissions: list[str],
    *,
    actor: Any,
    request: Any,
) -> AccountType:
    assert_permission_organization_scope(
        request,
        permission="access:write",
        account_kinds={AccountType.AccountKind.STAFF},
    )
    account_type = get_account_type(account_type.pk, for_update=True)
    if account_type.is_owner_type:
        raise ConflictException(
            _("The protected system owner permissions cannot be changed."),
            code="protected_account_type",
        )
    before = sorted(account_type.permission_rows.values_list("permission", flat=True))
    _replace_permissions(account_type, permissions)
    after = sorted(account_type.permission_rows.values_list("permission", flat=True))
    audit_log(
        actor=actor,
        action="update",
        resource_type="access.account_type_permissions",
        resource_id=account_type.pk,
        before={"permissions": before},
        after={"permissions": after},
        request=request,
    )
    return account_type_queryset().get(pk=account_type.pk)


@transaction.atomic
def assign_account_type(
    *,
    account_type: AccountType,
    principal_kind: str,
    principal_id: int,
    branch_id: int,
    department_id: int | None,
    actor: Any,
    request: Any,
) -> RoleMembership:
    account_type = get_account_type(account_type.pk, for_update=True)
    if not account_type.is_active:
        raise ConflictException(_("Inactive account types cannot be assigned."), code="account_type_inactive")
    principal_kind = validate_account_kind(principal_kind)
    if principal_kind != account_type.account_kind:
        raise ValidationException(
            _("Principal kind must match the account type."),
            code="principal_kind_mismatch",
            fields={"principal_kind": [_("This principal kind does not match the account type.")]},
        )
    if account_type.is_owner_type:
        # An owner assignment is organization-wide regardless of the branch
        # column retained for compatibility. A branch-scoped access writer must
        # never mint tenant-wide authority through that row.
        assert_permission_organization_scope(
            request,
            permission="access:write",
            account_kinds={AccountType.AccountKind.STAFF},
        )
    assert_permission_membership_scope(
        request,
        permission="access:write",
        branch_id=branch_id,
        department_id=department_id,
        account_kinds={AccountType.AccountKind.STAFF},
    )
    branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
    if branch is None:
        raise NotFoundException(_("Branch not found."), code="branch_not_found")
    department = None
    if department_id is not None:
        department = Department.objects.filter(pk=department_id, branch_id=branch_id, is_active=True).first()
        if department is None:
            raise ValidationException(
                _("Department must be active and belong to the selected branch."),
                code="department_branch_mismatch",
                fields={"department": [_("Choose a department in the selected branch.")]},
            )
    principal = resolve_principal(principal_kind, principal_id)
    principal_branch_id = getattr(principal, "branch_id", None)
    if (
        principal_kind in {AccountType.AccountKind.TEACHER, AccountType.AccountKind.STUDENT}
        and principal_branch_id != branch_id
    ):
        raise ValidationException(
            _("Principal belongs to another branch."),
            code="principal_branch_mismatch",
            fields={"principal_id": [_("Choose a principal in the selected branch.")]},
        )

    membership_queryset = RoleMembership.objects.filter(
        user_id=principal.user_id,
        account_type=account_type,
        branch_id=branch_id,
    )
    membership = (
        membership_queryset.filter(department__isnull=True).first()
        if department_id is None
        else membership_queryset.filter(department_id=department_id).first()
    )
    created = membership is None
    if membership is None:
        membership = RoleMembership.objects.create(
            user_id=principal.user_id,
            account_type=account_type,
            role=account_type.compatibility_role,
            branch=branch,
            department=department,
            granted_by=actor,
        )
    elif membership.revoked_at is None:
        raise ConflictException(_("This account type is already assigned."), code="assignment_exists")
    else:
        membership.role = account_type.compatibility_role
        membership.revoked_at = None
        membership.granted_by = actor
        membership.save(update_fields=("role", "revoked_at", "granted_by"))

    audit_log(
        actor=actor,
        action="create" if created else "update",
        resource_type="access.account_type_assignment",
        resource_id=membership.pk,
        after=_assignment_snapshot(membership, principal_kind, principal_id),
        request=request,
    )
    return assignment_queryset(request, permission="access:write").get(pk=membership.pk)


@transaction.atomic
def revoke_account_type_assignment(
    membership: RoleMembership,
    *,
    actor: Any,
    request: Any,
) -> None:
    locked_membership = (
        RoleMembership.objects.select_for_update()
        .select_related("account_type")
        .filter(pk=membership.pk, revoked_at__isnull=True, account_type__isnull=False)
        .first()
    )
    if locked_membership is None:
        raise NotFoundException(_("Assignment not found."), code="assignment_not_found")
    membership = locked_membership
    assert_permission_membership_scope(
        request,
        permission="access:write",
        branch_id=membership.branch_id,
        department_id=membership.department_id,
        account_kinds={AccountType.AccountKind.STAFF},
    )
    account_type = cast(AccountType, membership.account_type)
    if account_type.is_owner_type:
        assert_permission_organization_scope(
            request,
            permission="access:write",
            account_kinds={AccountType.AccountKind.STAFF},
        )
        other_owner_exists = (
            RoleMembership.objects.filter(
                account_type=account_type,
                revoked_at__isnull=True,
            )
            .exclude(pk=membership.pk)
            .exists()
        )
        if not other_owner_exists:
            raise ConflictException(
                _("The final system owner assignment cannot be revoked."),
                code="last_owner_assignment",
            )
    principal_kind, principal_id = principal_identity(membership)
    before = _assignment_snapshot(membership, principal_kind, principal_id)
    membership_id = membership.pk
    membership.delete()
    audit_log(
        actor=actor,
        action="delete",
        resource_type="access.account_type_assignment",
        resource_id=membership_id,
        before=before,
        request=request,
    )


def effective_permissions_for_principal(principal_kind: str, principal_id: int) -> dict[str, Any]:
    principal_kind = validate_account_kind(principal_kind)
    principal = resolve_principal(principal_kind, principal_id)
    memberships = list(
        RoleMembership.objects.filter(revoked_at__isnull=True, user_id=principal.user_id)
        .filter(
            Q(
                account_type__account_kind=principal_kind,
                account_type__is_active=True,
            )
            | Q(
                account_type__isnull=True,
                role__in=_LEGACY_ROLES_BY_PRINCIPAL_KIND[principal_kind],
            )
        )
        .select_related("account_type")
    )
    type_ids = {membership.account_type_id for membership in memberships if membership.account_type_id}
    permissions = set(
        AccountTypePermission.objects.filter(account_type_id__in=type_ids).values_list(
            "permission", flat=True
        )
    )
    fallback_roles = {membership.role for membership in memberships if membership.account_type_id is None}
    if fallback_roles:
        overrides: dict[str, dict[str, str]] = {}
        for role, permission, effect in RolePermissionOverride.objects.filter(
            role__in=fallback_roles
        ).values_list("role", "permission", "effect"):
            overrides.setdefault(role, {})[permission] = effect
        for role in fallback_roles:
            granted, revoked = _role_grant_revoke(role, overrides)
            permissions.update(
                permission for permission in granted if _code_allowed(granted, revoked, permission)
            )
    return {
        "principal_kind": principal_kind,
        "principal_id": principal_id,
        "permissions": sorted(permissions),
        "account_types": [
            {
                "id": membership.account_type_id,
                "name": cast(AccountType, membership.account_type).name,
                "slug": cast(AccountType, membership.account_type).slug,
                "branch": membership.branch_id,
                "department": membership.department_id,
            }
            for membership in memberships
            if membership.account_type_id is not None
        ],
        "legacy_fallback_roles": sorted(fallback_roles),
    }


def resolve_principal(principal_kind: str, principal_id: int) -> Any:
    principal_kind = validate_account_kind(principal_kind)
    if principal_id <= 0:
        raise ValidationException(
            _("Principal id must be a positive integer."),
            code="validation_error",
            fields={"principal_id": [_("Must be a positive integer.")]},
        )
    app_label, model_name = _PRINCIPAL_MODELS[principal_kind]
    model = django_apps.get_model(app_label, model_name)
    principal = model.objects.select_related("user").filter(pk=principal_id, is_active=True).first()
    if principal is None:
        raise NotFoundException(_("Principal not found."), code="principal_not_found")
    return principal


def assignment_principal_identity(membership: RoleMembership) -> tuple[str, int | None]:
    """Return the assignment's expected principal kind and its profile id, if present.

    Historical role memberships can point at a plain compatibility ``User`` whose
    role-native profile was never created.  Reads must keep those security grants
    visible so an owner can audit them, but must never claim that an unrelated
    profile on the same user is the assignment's principal.
    """
    account_type = cast(AccountType, membership.account_type)
    principal_kind = account_type.account_kind
    relation = _PRINCIPAL_RELATIONS.get(principal_kind)
    principal = getattr(membership.user, relation, None) if relation is not None else None
    return principal_kind, principal.pk if principal is not None else None


def principal_identity(membership: RoleMembership) -> tuple[str, int]:
    principal_kind, principal_id = assignment_principal_identity(membership)
    if principal_id is not None:
        return principal_kind, principal_id
    raise ConflictException(
        _("Assignment has no compatible role profile."),
        code="principal_missing",
    )


def _replace_permissions(account_type: AccountType, permissions: list[str]) -> None:
    normalized = [
        validate_account_type_permission(permission, account_type=account_type) for permission in permissions
    ]
    if len(normalized) != len(set(normalized)):
        raise ValidationException(
            _("Permission grants must be unique."),
            code="validation_error",
            fields={"permissions": [_("Remove duplicate permission codes.")]},
        )
    account_type.permission_rows.all().delete()
    AccountTypePermission.objects.bulk_create(
        [AccountTypePermission(account_type=account_type, permission=permission) for permission in normalized]
    )


def _validate_type_uniqueness(*, name: str, slug: str, exclude_pk: int | None = None) -> None:
    queryset = AccountType.objects.all()
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    fields: dict[str, list[Any]] = {}
    if queryset.filter(name__iexact=name).exists():
        fields["name"] = [_("An account type with this name already exists.")]
    if queryset.filter(slug=slug).exists():
        fields["slug"] = [_("An account type with this slug already exists.")]
    if fields:
        raise ValidationException(_("Account type already exists."), fields=fields)


def _type_snapshot(account_type: AccountType) -> dict[str, Any]:
    return {
        "name": account_type.name,
        "slug": account_type.slug,
        "account_kind": account_type.account_kind,
        "description": account_type.description,
        "is_active": account_type.is_active,
        "is_system": account_type.is_system,
        "permissions": sorted(account_type.permission_rows.values_list("permission", flat=True)),
    }


def _assignment_snapshot(
    membership: RoleMembership,
    principal_kind: str,
    principal_id: int,
) -> dict[str, Any]:
    return {
        "account_type_id": membership.account_type_id,
        "principal_kind": principal_kind,
        "principal_id": principal_id,
        "branch_id": membership.branch_id,
        "department_id": membership.department_id,
    }
