"""ORM-backed guardian repository (parent↔student links)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.parents.interfaces.repositories import IGuardianRepository
from apps.parents.models import Guardian
from apps.parents.repositories.scoping import scope_rows
from core.repositories import BaseRepository


class GuardianRepository(BaseRepository[Guardian], IGuardianRepository):
    model = Guardian

    def get_queryset(self) -> QuerySet[Guardian]:
        return (
            Guardian.objects.filter(revoked_at__isnull=True)
            .select_related(
                "parent__user",
                "student__user",
                "student__branch",
                "student__current_cohort",
            )
            .defer(
                "custody_notes",
                "parent__notes",
                "student__medical_notes",
                "student__emergency_contacts",
            )
        )

    def scoped(self, *, user, roles, permission: str) -> QuerySet[Guardian]:
        return scope_rows(
            self.get_queryset(),
            user=user,
            roles=roles,
            permission=permission,
            own_filter={"parent__user": user},
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
    ) -> Guardian | None:
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()
