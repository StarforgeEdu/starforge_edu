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
        return PickupAuthorization.objects.select_related("student__user")

    def scoped(self, *, user, roles) -> QuerySet[PickupAuthorization]:
        # A parent sees only their own children's pickup rows (the parents:read
        # grant alone must not expose the whole tenant).
        return scope_rows(
            self.get_queryset(),
            user=user,
            roles=roles,
            own_filter={"student__guardians__parent__user": user},
        )

    def get_scoped(self, *, user, roles, pk: int) -> PickupAuthorization | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()
