"""ORM-backed pickup-authorization repository."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.parents.interfaces.repositories import IPickupRepository
from apps.parents.models import PickupAuthorization
from apps.parents.repositories.scoping import scope_rows
from core.repositories import BaseRepository


class PickupRepository(BaseRepository[PickupAuthorization], IPickupRepository):
    model = PickupAuthorization

    def get_queryset(self) -> QuerySet[PickupAuthorization]:
        return PickupAuthorization.objects.select_related("student__user").defer(
            "student__medical_notes",
            "student__emergency_contacts",
        )

    def scoped(self, *, user, roles, permission: str) -> QuerySet[PickupAuthorization]:
        # A parent sees only their own children's pickup rows (the parents:read
        # grant alone must not expose the whole tenant).
        return scope_rows(
            self.get_queryset(),
            user=user,
            roles=roles,
            permission=permission,
            own_filter={
                "student__guardians__parent__user": user,
                "student__guardians__revoked_at__isnull": True,
            },
            branch_field="student__branch_id",
            department_field="student__current_cohort__department_id",
        )

    def get_scoped(
        self,
        *,
        user,
        roles,
        permission: str,
        pk: int,
    ) -> PickupAuthorization | None:
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()
