"""ParentService — parent CRUD + linked-students + parent self-service."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.parents.dto.parent_dto import ParentCreateDTO
from apps.parents.interfaces.repositories import IParentRepository
from apps.parents.interfaces.services import IParentService
from apps.parents.models import ParentProfile
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.historical_scope import ScopeAttributionStatus
from core.permissions import has_permission_code
from core.scoping import permission_membership_is_unscoped, permission_membership_scopes

_UPDATABLE = ("workplace", "notes")
_IDENTITY_FIELDS = (
    "first_name",
    "last_name",
    "middle_name",
    "phone",
    "email",
    "birthdate",
    "gender",
)


def _audit_scopes_for_parent(parent: ParentProfile):
    """One immutable audit scope per active family boundary, never an inferred join."""
    from apps.audit.scopes import scoped_audit_scope, unresolved_audit_scope
    from apps.parents.models import Guardian

    scope_rows: set[tuple[int, int | None]] = set(
        Guardian.objects.filter(parent=parent, revoked_at__isnull=True).values_list(
            "student__branch_id",
            "student__current_cohort__department_id",
        )
    )
    if not scope_rows and parent.branch_at_creation_id is not None:
        scope_rows.add((parent.branch_at_creation_id, parent.department_at_creation_id))
    if not scope_rows:
        return (unresolved_audit_scope(),)
    return tuple(
        scoped_audit_scope(branch_id, department_id)
        for branch_id, department_id in sorted(
            scope_rows,
            key=lambda row: (row[0], row[1] or 0),
        )
    )


class ParentService(IParentService):
    def __init__(self, parents: IParentRepository) -> None:
        self._parents = parents

    def scoped_list(self, *, user, roles, permission: str) -> QuerySet[ParentProfile]:
        return self._parents.scoped(user=user, roles=roles, permission=permission)

    def get(self, *, user, roles, permission: str, pk: int) -> ParentProfile | None:
        return self._parents.get_scoped(
            user=user,
            roles=roles,
            permission=permission,
            pk=pk,
        )

    def create(self, data: ParentCreateDTO, *, user, roles) -> ParentProfile:
        from apps.parents.services import create_parent

        branch, department, attribution_status = self._resolve_creation_scope(
            data,
            user=user,
            roles=roles,
        )
        if data.notes and not self._boundary_permission_allows(
            user=user,
            roles=roles,
            permission="safeguarding:write",
            branch_id=branch.pk if branch is not None else None,
            department_id=department.pk if department is not None else None,
        ):
            raise PermissionException(code="out_of_scope")
        return create_parent(
            username=data.username,
            phone=data.phone,
            email=data.email,
            first_name=data.first_name,
            last_name=data.last_name,
            middle_name=data.middle_name,
            birthdate=data.birthdate,
            gender=data.gender,
            workplace=data.workplace,
            notes=data.notes,
            branch_at_creation=branch,
            department_at_creation=department,
            attribution_status=attribution_status,
            created_by=user,
        )

    @transaction.atomic
    def update(self, parent: ParentProfile, changes: dict[str, Any]) -> ParentProfile:
        from apps.users.models import User
        from core.identity_lifecycle import assert_exclusive_role_bridge

        unsupported = sorted(set(changes) - set(_IDENTITY_FIELDS) - set(_UPDATABLE))
        if unsupported:
            raise ValidationException(
                _("Unsupported parent-account field."),
                code="validation_error",
                fields={field: [_("This field is not supported.")] for field in unsupported},
            )
        parent = (
            ParentProfile.objects.select_for_update(of=("self",))
            .select_related("user")
            .defer("notes")
            .get(pk=parent.pk)
        )
        identity_changes = {field: changes[field] for field in _IDENTITY_FIELDS if field in changes}
        if identity_changes:
            User.objects.select_for_update().get(pk=parent.user_id)
            assert_exclusive_role_bridge(parent, principal_kind="parent")
            from apps.users.services import prepare_role_identity, update_role_identity

            if {"phone", "email"} & identity_changes.keys():
                normalized = prepare_role_identity(
                    phone=identity_changes.get("phone", parent.phone),
                    email=identity_changes.get("email", parent.email),
                    first_name=identity_changes.get("first_name", parent.first_name),
                    last_name=identity_changes.get("last_name", parent.last_name),
                    middle_name=identity_changes.get("middle_name", parent.middle_name),
                )
                if not normalized["phone"] and not normalized["email"]:
                    raise ValidationException(
                        _("Keep a phone number or email on the parent account."),
                        code="identifier_required",
                        fields={
                            "phone": [_("A phone or email is required.")],
                            "email": [_("A phone or email is required.")],
                        },
                    )
            try:
                update_role_identity(parent, identity_changes)
            except IntegrityError as exc:
                conflicting_fields = sorted({"phone", "email"} & identity_changes.keys())
                if not conflicting_fields:
                    raise
                raise ValidationException(
                    _("This contact already belongs to another parent account."),
                    code="duplicate_account",
                    fields={field: [_("Choose a unique contact value.")] for field in conflicting_fields},
                ) from exc
        for field in _UPDATABLE:
            if field in changes:
                setattr(parent, field, changes[field])
        if any(field in changes for field in _UPDATABLE):
            parent.save()
        return parent

    @transaction.atomic
    def deactivate(self, parent: ParentProfile, *, actor) -> ParentProfile:
        """Disable parent login without erasing legal family-link history."""
        from apps.audit.services import audit_log
        from apps.users.models import User
        from apps.users.services import revoke_role_account_access
        from core.identity_lifecycle import assert_exclusive_role_bridge

        parent = (
            ParentProfile.objects.select_for_update(of=("self",))
            .select_related("user")
            .defer("notes")
            .get(pk=parent.pk)
        )
        User.objects.select_for_update().get(pk=parent.user_id)
        assert_exclusive_role_bridge(parent, principal_kind="parent")
        if not parent.is_active:
            return parent

        revoke_role_account_access(parent)
        for scope in _audit_scopes_for_parent(parent):
            audit_log(
                actor=actor,
                action="update",
                resource_type="parents.ParentProfile",
                resource_id=parent.pk,
                before={"is_active": True},
                after={"is_active": False},
                scope=scope,
            )
        return parent

    @transaction.atomic
    def issue_credentials(self, parent: ParentProfile, *, actor) -> dict[str, Any]:
        from apps.users.models import User
        from apps.users.services import issue_role_credentials
        from core.exceptions import ConflictException
        from core.identity_lifecycle import assert_exclusive_role_bridge

        parent = (
            ParentProfile.objects.select_for_update(of=("self",))
            .select_related("user")
            .defer("notes")
            .get(pk=parent.pk)
        )
        user = User.objects.select_for_update().get(pk=parent.user_id)
        assert_exclusive_role_bridge(parent, principal_kind="parent")
        if not parent.is_active or not user.is_active:
            raise ConflictException(
                _("Inactive parent accounts cannot receive new credentials."),
                code="account_inactive",
            )
        return issue_role_credentials(
            parent,
            actor=actor,
            resource_type="parents.ParentProfile",
            audit_scopes=_audit_scopes_for_parent(parent),
        )

    def students(
        self,
        parent: ParentProfile,
        *,
        user=None,
        roles=None,
        permission: str = "parents:read",
    ) -> QuerySet:
        return self._parents.students_for(
            parent,
            user=user,
            roles=roles,
            permission=permission,
        )

    def assert_manage_scope(
        self,
        parent: ParentProfile,
        *,
        user,
        roles,
        permission: str,
    ) -> None:
        if not self._parents.all_students_in_scope(
            parent,
            user=user,
            roles=roles,
            permission=permission,
        ):
            raise NotFoundException(code="not_found")

    def scope_allows(self, parent: ParentProfile, *, user, roles, permission: str) -> bool:
        if getattr(user, "is_superuser", False):
            return True
        if not has_permission_code(roles, permission):
            return False
        if permission_membership_is_unscoped(
            roles=roles,
            permission=permission,
            account_kinds={"staff"},
        ):
            return True
        return self._parents.all_students_in_scope(
            parent,
            user=user,
            roles=roles,
            permission=permission,
            lock_parent=False,
        )

    def require_profile(self, user) -> ParentProfile:
        parent = self._parents.profile_for(user)
        if parent is None:
            raise NotFoundException(_("You do not have a parent profile."), code="not_a_parent")
        return parent

    def child_or_404(self, parent: ParentProfile, student_id: int):
        student = self._parents.students_for(parent).filter(pk=student_id).first()
        if student is None:
            raise NotFoundException(_("That is not one of your children."), code="not_your_child")
        return student

    @staticmethod
    def _resolve_creation_scope(data: ParentCreateDTO, *, user, roles):
        """Resolve one immutable scope from the exact ``parents:write`` grant."""
        from apps.org.models import Branch, Department

        permission = "parents:write"
        is_organization_wide = getattr(user, "is_superuser", False) or (
            permission_membership_is_unscoped(
                roles=roles,
                permission=permission,
                account_kinds={"staff"},
            )
        )
        if is_organization_wide:
            if data.branch_id is None:
                if data.department_id is not None:
                    raise ValidationException(
                        _("Choose a branch for the department."),
                        code="validation_error",
                        fields={"branch": ["This field is required."]},
                    )
                # An owner may intentionally create an organization-level draft,
                # but it remains unresolved and only organization-wide operators
                # may attach it until a reviewed attribution is supplied.
                return None, None, ScopeAttributionStatus.UNRESOLVED
            branch = Branch.objects.filter(
                pk=data.branch_id,
                is_active=True,
                archived_at__isnull=True,
            ).first()
            if branch is None:
                raise NotFoundException(code="not_found")
            department = None
            if data.department_id is not None:
                department = Department.objects.filter(
                    pk=data.department_id,
                    branch=branch,
                    is_active=True,
                ).first()
                if department is None:
                    raise NotFoundException(code="not_found")
            return branch, department, ScopeAttributionStatus.CAPTURED

        scopes = tuple(
            scope
            for scope in permission_membership_scopes(
                roles=roles,
                permission=permission,
                account_kinds={"staff"},
            )
            if not scope.is_organization_wide
        )
        if not scopes:
            raise PermissionException(code="out_of_scope")

        allowed_branch_ids = {scope.branch_id for scope in scopes}
        branch_id = data.branch_id
        if branch_id is None:
            if len(allowed_branch_ids) != 1:
                raise ValidationException(
                    _("Choose the branch for this parent."),
                    code="validation_error",
                    fields={"branch": ["This field is required for multi-branch access."]},
                )
            branch_id = next(iter(allowed_branch_ids))
        elif branch_id not in allowed_branch_ids:
            raise NotFoundException(code="not_found")

        branch = Branch.objects.filter(
            pk=branch_id,
            is_active=True,
            archived_at__isnull=True,
        ).first()
        if branch is None:
            raise NotFoundException(code="not_found")
        branch_scopes = tuple(scope for scope in scopes if scope.branch_id == branch_id)

        department = None
        department_id = data.department_id
        if department_id is not None:
            if not any(
                scope.department_id is None or scope.department_id == department_id for scope in branch_scopes
            ):
                raise NotFoundException(code="not_found")
            department = Department.objects.filter(
                pk=department_id,
                branch=branch,
                is_active=True,
            ).first()
            if department is None:
                raise NotFoundException(code="not_found")
        elif not any(scope.department_id is None for scope in branch_scopes):
            department_ids = {
                scope.department_id for scope in branch_scopes if scope.department_id is not None
            }
            if len(department_ids) != 1:
                raise ValidationException(
                    _("Choose the department for this parent."),
                    code="validation_error",
                    fields={"department": ["This field is required for multi-department access."]},
                )
            department_id = next(iter(department_ids))
            department = Department.objects.filter(
                pk=department_id,
                branch=branch,
                is_active=True,
            ).first()
            if department is None:
                raise NotFoundException(code="not_found")

        return branch, department, ScopeAttributionStatus.CAPTURED

    @staticmethod
    def _boundary_permission_allows(
        *,
        user,
        roles,
        permission: str,
        branch_id: int | None,
        department_id: int | None,
    ) -> bool:
        if getattr(user, "is_superuser", False):
            return True
        for scope in permission_membership_scopes(
            roles=roles,
            permission=permission,
            account_kinds={"staff"},
        ):
            if scope.is_organization_wide:
                return True
            if branch_id is None or scope.branch_id != branch_id:
                continue
            if scope.department_id is None or scope.department_id == department_id:
                return True
        return False
