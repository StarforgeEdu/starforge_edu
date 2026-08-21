"""PickupService — pickup-authorization CRUD."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.parents.dto.parent_dto import PickupCreateDTO
from apps.parents.interfaces.repositories import IPickupRepository
from apps.parents.interfaces.services import IPickupService
from apps.parents.models import PickupAuthorization
from apps.parents.repositories.scoping import scope_rows
from core.exceptions import ConflictException, NotFoundException, ValidationException

_SCALARS = ("full_name", "phone", "relationship", "is_active")


class PickupService(IPickupService):
    def __init__(self, pickups: IPickupRepository) -> None:
        self._pickups = pickups

    def scoped_list(self, *, user, roles, permission: str) -> QuerySet[PickupAuthorization]:
        return self._pickups.scoped(user=user, roles=roles, permission=permission)

    def get(self, *, user, roles, permission: str, pk: int) -> PickupAuthorization | None:
        return self._pickups.get_scoped(
            user=user,
            roles=roles,
            permission=permission,
            pk=pk,
        )

    @transaction.atomic
    def create(self, data: PickupCreateDTO, *, user, roles) -> PickupAuthorization:
        if not data.is_active:
            raise ValidationException(
                _("New pickup authorizations must be active."),
                code="validation_error",
                fields={"is_active": [_("Deactivate an existing authorization instead.")]},
            )
        student = self._resolve_student(data.student_id, user=user, roles=roles)
        pickup = PickupAuthorization.objects.create(
            student=student,
            full_name=data.full_name,
            phone=data.phone,
            relationship=data.relationship,
            is_active=data.is_active,
        )
        self._audit_change(
            pickup,
            actor=user,
            action="create",
            before=None,
            after={"student_id": student.pk, "is_active": True},
        )
        return pickup

    @transaction.atomic
    def update(
        self,
        pickup: PickupAuthorization,
        changes: dict[str, Any],
        *,
        user,
        roles,
        actor,
    ) -> PickupAuthorization:
        unsupported = sorted(set(changes) - set(_SCALARS))
        if unsupported:
            raise ValidationException(
                _("Unsupported pickup-authorization field."),
                code="validation_error",
                fields={field: [_("This field is not supported.")] for field in unsupported},
            )
        locked_pickup = (
            PickupAuthorization.objects.select_for_update(of=("self",))
            .select_related("student__current_cohort")
            .filter(pk=pickup.pk)
            .first()
        )
        if locked_pickup is None:
            raise NotFoundException(code="not_found")
        pickup = locked_pickup
        if not pickup.is_active:
            raise ConflictException(
                _("A deactivated pickup authorization cannot be changed."),
                code="pickup_authorization_inactive",
            )
        # Re-check the current child after locking the row. A transfer between
        # the view's first lookup and this write must not retain stale scope.
        self._resolve_student(pickup.student_id, user=user, roles=roles)
        changed_fields = sorted(field for field in changes if field != "is_active" or changes[field] is False)
        mutable_fields: list[str] = []
        for field in _SCALARS:
            if field in changes and field != "is_active":
                setattr(pickup, field, changes[field])
                mutable_fields.append(field)
        if mutable_fields:
            pickup.save(update_fields=mutable_fields)
        if changes.get("is_active") is False:
            return self._deactivate_locked(
                pickup,
                actor=actor,
                changed_fields=changed_fields,
            )
        if changed_fields:
            self._audit_change(
                pickup,
                actor=actor,
                action="update",
                before={"student_id": pickup.student_id, "is_active": True},
                after={
                    "student_id": pickup.student_id,
                    "is_active": True,
                    # Do not copy a pickup person's name or phone into the audit
                    # trail. The changed field names are enough to establish the
                    # mutation while the retained row remains authoritative.
                    "changed_fields": changed_fields,
                },
            )
        return pickup

    @transaction.atomic
    def deactivate(self, pickup: PickupAuthorization, *, user, roles, actor) -> PickupAuthorization:
        locked_pickup = (
            PickupAuthorization.objects.select_for_update(of=("self",))
            .select_related("student__current_cohort")
            .filter(pk=pickup.pk)
            .first()
        )
        if locked_pickup is None:
            raise NotFoundException(code="not_found")
        pickup = locked_pickup
        self._resolve_student(pickup.student_id, user=user, roles=roles)
        if not pickup.is_active:
            return pickup
        return self._deactivate_locked(pickup, actor=actor)

    @staticmethod
    def _deactivate_locked(
        pickup: PickupAuthorization,
        *,
        actor,
        changed_fields: list[str] | None = None,
    ) -> PickupAuthorization:
        pickup.is_active = False
        pickup.deactivated_at = timezone.now()
        pickup.deactivated_by = actor
        pickup.save(update_fields=["is_active", "deactivated_at", "deactivated_by"])
        PickupService._audit_change(
            pickup,
            actor=actor,
            action="update",
            before={"student_id": pickup.student_id, "is_active": True},
            after={
                "student_id": pickup.student_id,
                "is_active": False,
                "changed_fields": changed_fields or ["is_active"],
            },
        )
        return pickup

    @staticmethod
    def _audit_change(
        pickup: PickupAuthorization,
        *,
        actor,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
    ) -> None:
        from apps.audit.scopes import scoped_audit_scope
        from apps.audit.services import audit_log

        student = pickup.student
        cohort = student.current_cohort
        audit_log(
            actor=actor,
            action=action,
            resource_type="parents.PickupAuthorization",
            resource_id=pickup.pk,
            before=before,
            after=after,
            scope=scoped_audit_scope(
                student.branch_id,
                cohort.department_id if cohort is not None else None,
            ),
        )

    @staticmethod
    def _resolve_student(student_id: int, *, user, roles):
        from apps.students.models import StudentProfile
        from apps.users.models import User
        from core.identity_lifecycle import assert_exclusive_role_bridge

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
        User.objects.select_for_update().get(pk=student.user_id)
        assert_exclusive_role_bridge(student, principal_kind="student")
        return student
