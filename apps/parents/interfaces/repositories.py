"""Parent-domain repository ports.

Staff scoping follows active branch/department memberships; a parent sees only
their own rows. Out-of-scope detail lookups return ``None`` to avoid id leaks.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.parents.models import Guardian, ParentProfile, PickupAuthorization
from core.interfaces import IBaseRepository


class IParentRepository(IBaseRepository[ParentProfile]):
    def scoped(self, *, user, roles, permission: str) -> QuerySet[ParentProfile]:
        """Parents visible through membership scope or self ownership."""
        raise NotImplementedError

    def get_scoped(self, *, user, roles, permission: str, pk: int) -> ParentProfile | None:
        """A single in-scope parent by pk, or None (out-of-scope reads 404, no leak)."""
        raise NotImplementedError

    def profile_for(self, user) -> ParentProfile | None:
        """The signed-in user's own parent profile (self-service), or None."""
        raise NotImplementedError

    def students_for(
        self,
        parent: ParentProfile,
        *,
        user=None,
        roles=None,
        permission: str = "parents:read",
    ) -> QuerySet:
        """The parent's linked students (the sanctioned parents→students link)."""
        raise NotImplementedError

    def all_students_in_scope(
        self,
        parent: ParentProfile,
        *,
        user,
        roles,
        permission: str,
        lock_parent: bool = True,
    ) -> bool:
        """Whether the permission covers the parent and every linked child."""
        raise NotImplementedError


class IGuardianRepository(IBaseRepository[Guardian]):
    def scoped(self, *, user, roles, permission: str) -> QuerySet[Guardian]:
        raise NotImplementedError

    def get_scoped(self, *, user, roles, permission: str, pk: int) -> Guardian | None:
        raise NotImplementedError


class IPickupRepository(IBaseRepository[PickupAuthorization]):
    def scoped(self, *, user, roles, permission: str) -> QuerySet[PickupAuthorization]:
        raise NotImplementedError

    def get_scoped(
        self,
        *,
        user,
        roles,
        permission: str,
        pk: int,
    ) -> PickupAuthorization | None:
        raise NotImplementedError
