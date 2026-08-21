"""GuardianService — parent↔student links (create + delete; no update by design)."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.parents.dto.parent_dto import GuardianCreateDTO
from apps.parents.interfaces.repositories import IGuardianRepository
from apps.parents.interfaces.services import IGuardianService
from apps.parents.models import Guardian
from apps.parents.repositories.scoping import (
    attributed_unassigned_parent_scope_q,
    scope_rows,
)
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.scoping import permission_membership_scopes


class GuardianService(IGuardianService):
    def __init__(self, guardians: IGuardianRepository) -> None:
        self._guardians = guardians

    def scoped_list(self, *, user, roles, permission: str) -> QuerySet[Guardian]:
        return self._guardians.scoped(user=user, roles=roles, permission=permission)

    def get(self, *, user, roles, permission: str, pk: int) -> Guardian | None:
        return self._guardians.get_scoped(
            user=user,
            roles=roles,
            permission=permission,
            pk=pk,
        )

    @transaction.atomic
    def create(self, data: GuardianCreateDTO, *, user, roles) -> Guardian:
        from apps.parents.services import link_guardian

        parent = self._resolve_parent(data.parent_id, user=user, roles=roles)
        student = self._resolve_student(data.student_id, user=user, roles=roles)
        if data.custody_notes and not self._can_write_custody_notes(
            user=user,
            roles=roles,
            student=student,
        ):
            raise PermissionException(code="out_of_scope")
        return link_guardian(
            parent=parent,
            student=student,
            relationship=self._validate_relationship(data.relationship),
            is_primary=data.is_primary,
            custody_notes=data.custody_notes,
            actor=user,
        )

    @transaction.atomic
    def revoke(self, guardian: Guardian, *, user, roles, actor) -> Guardian:
        """Append a revocation instead of deleting legal/custody history."""
        from apps.audit.scopes import scoped_audit_scope
        from apps.audit.services import audit_log
        from apps.parents.models import ParentProfile
        from apps.students.models import StudentProfile

        # Parent is the serialization root used by create and parent-wide
        # mutation paths. The relationship and child are then locked in stable
        # order before current scope is re-evaluated.
        ParentProfile.objects.select_for_update().get(pk=guardian.parent_id)
        locked_guardian = (
            Guardian.objects.select_for_update(of=("self",))
            .select_related("student__current_cohort")
            .filter(pk=guardian.pk, revoked_at__isnull=True)
            .first()
        )
        if locked_guardian is None:
            raise NotFoundException(code="not_found")
        guardian = locked_guardian
        student = (
            StudentProfile.objects.select_for_update(of=("self",))
            .select_related("current_cohort")
            .get(pk=guardian.student_id)
        )
        if not scope_rows(
            StudentProfile.objects.filter(pk=student.pk),
            user=user,
            roles=roles,
            permission="parents:write",
            own_filter={
                "guardians__parent__user": user,
                "guardians__revoked_at__isnull": True,
            },
            branch_field="branch_id",
            department_field="current_cohort__department_id",
        ).exists():
            raise NotFoundException(code="not_found")

        guardian.revoked_at = timezone.now()
        guardian.revoked_by = actor
        guardian.save(update_fields=["revoked_at", "revoked_by"])
        self._revoke_orphaned_parent_memberships(parent_id=guardian.parent_id)
        cohort = student.current_cohort
        audit_log(
            actor=actor,
            action="update",
            resource_type="parents.Guardian",
            resource_id=guardian.pk,
            before={
                "parent_id": guardian.parent_id,
                "student_id": guardian.student_id,
                "revoked_at": None,
            },
            after={
                "parent_id": guardian.parent_id,
                "student_id": guardian.student_id,
                "revoked_at": guardian.revoked_at.isoformat(),
            },
            scope=scoped_audit_scope(
                student.branch_id,
                cohort.department_id if cohort is not None else None,
            ),
        )
        return guardian

    @staticmethod
    def _revoke_orphaned_parent_memberships(*, parent_id: int) -> None:
        """Retire parent grants no active child relationship supports exactly."""
        from apps.access.models import AccountType
        from apps.parents.models import ParentProfile
        from apps.students.models import StudentProfile
        from apps.users.models import RoleMembership
        from apps.users.services import bump_token_version
        from core.permissions import Role

        parent = ParentProfile.objects.only("user_id").get(pk=parent_id)
        active_student_ids = sorted(
            set(
                Guardian.objects.filter(
                    parent_id=parent_id,
                    revoked_at__isnull=True,
                ).values_list("student_id", flat=True)
            )
        )
        active_scopes = set(
            StudentProfile.objects.select_for_update(of=("self",))
            .filter(pk__in=active_student_ids)
            .order_by("pk")
            .values_list("branch_id", "current_cohort__department_id")
        )
        active_branch_ids = {branch_id for branch_id, _department_id in active_scopes}
        memberships = list(
            RoleMembership.objects.filter(
                user_id=parent.user_id,
                revoked_at__isnull=True,
            )
            .filter(
                Q(account_type__account_kind=AccountType.AccountKind.PARENT)
                | Q(account_type__isnull=True, role=Role.PARENT)
            )
            .select_for_update(of=("self",))
            .order_by("pk")
        )
        orphaned_ids = [
            membership.pk
            for membership in memberships
            if (
                membership.branch_id is None
                or (membership.department_id is None and membership.branch_id not in active_branch_ids)
                or (
                    membership.department_id is not None
                    and (membership.branch_id, membership.department_id) not in active_scopes
                )
            )
        ]
        if orphaned_ids:
            RoleMembership.objects.filter(pk__in=orphaned_ids).update(revoked_at=timezone.now())
            # QuerySet.update intentionally avoids N post_save signals; one
            # token-version bump is enough to invalidate the stale graph.
            bump_token_version(parent.user_id)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _validate_relationship(value: str) -> str:
        if value not in Guardian.Relationship.values:
            raise ValidationException(
                _("Invalid relationship."),
                code="validation_error",
                fields={"relationship": ["Not a valid choice."]},
            )
        return value

    @staticmethod
    def _resolve_parent(parent_id: int, *, user, roles):
        from apps.parents.models import ParentProfile

        base = ParentProfile.objects.filter(is_active=True, user__is_active=True).annotate(
            _has_active_guardian=Exists(
                Guardian.objects.filter(parent_id=OuterRef("pk"), revoked_at__isnull=True)
            )
        )
        linked = scope_rows(
            base.filter(_has_active_guardian=True, guardianships__revoked_at__isnull=True),
            user=user,
            roles=roles,
            permission="parents:write",
            own_filter={"user": user},
            branch_field="guardianships__student__branch_id",
            department_field="guardianships__student__current_cohort__department_id",
        ).distinct()
        attributed_unassigned = (
            base.filter(_has_active_guardian=False)
            .filter(
                attributed_unassigned_parent_scope_q(
                    user=user,
                    roles=roles,
                    permission="parents:write",
                )
            )
            .distinct()
        )
        visible = (linked | attributed_unassigned).filter(pk=OuterRef("pk"))
        # Keep DISTINCT and relationship joins inside EXISTS. PostgreSQL does
        # not permit SELECT DISTINCT ... FOR UPDATE, so the outer, one-row
        # ParentProfile query owns the lock while the subquery proves scope.
        # This serializes every API link attempt before the Guardian is created.
        parent = (
            ParentProfile.objects.annotate(_is_visible=Exists(visible))
            .filter(pk=parent_id, _is_visible=True)
            .select_for_update(of=("self",))
            .first()
        )
        if parent is None:
            raise ValidationException(
                _("Invalid parent."), code="invalid_parent", fields={"parent": ["Not found."]}
            )
        return parent

    @staticmethod
    def _resolve_student(student_id: int, *, user, roles):
        from apps.students.models import StudentProfile

        def visible():
            return scope_rows(
                StudentProfile.objects.filter(
                    pk=student_id,
                    is_active=True,
                    user__is_active=True,
                ),
                user=user,
                roles=roles,
                permission="parents:write",
                own_filter={
                    "guardians__parent__user": user,
                    "guardians__revoked_at__isnull": True,
                },
                branch_field="branch_id",
                department_field="current_cohort__department_id",
            )

        if not visible().exists():
            raise ValidationException(
                _("Invalid student."), code="invalid_student", fields={"student": ["Not found."]}
            )
        # A branch transfer serializes on StudentProfile. Re-read after acquiring
        # that lock and repeat the exact scope query so a stale pre-lock branch
        # cannot authorize a new family relationship.
        student = (
            StudentProfile.objects.select_for_update(of=("self",))
            .select_related("user", "current_cohort")
            .filter(pk=student_id, is_active=True, user__is_active=True)
            .first()
        )
        if student is None or not visible().exists():
            raise ValidationException(
                _("Invalid student."), code="invalid_student", fields={"student": ["Not found."]}
            )
        return student

    @staticmethod
    def _can_write_custody_notes(*, user, roles, student) -> bool:
        if getattr(user, "is_superuser", False):
            return True
        department_id = (
            student.current_cohort.department_id if student.current_cohort_id is not None else None
        )
        for scope in permission_membership_scopes(
            roles=roles,
            permission="safeguarding:write",
            account_kinds={"staff"},
        ):
            if scope.is_organization_wide:
                return True
            if scope.branch_id != student.branch_id:
                continue
            if scope.department_id is None or scope.department_id == department_id:
                return True
        return False
