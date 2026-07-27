"""Parent-domain repository ports.

Scoping here is ROLE-based (staff see all; a parent sees only their own rows),
NOT branch-based — so these ports expose ``scoped``/``get_scoped`` instead of the
branch helpers used by the org-scoped apps.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.parents.models import Guardian, ParentProfile, PickupAuthorization
from core.interfaces import IBaseRepository


class IParentRepository(IBaseRepository[ParentProfile]):
    def scoped(self, *, user, roles) -> QuerySet[ParentProfile]:
        """Parents the caller may read: staff all, a parent only their own row."""
        raise NotImplementedError

    def get_scoped(self, *, user, roles, pk: int) -> ParentProfile | None:
        """A single in-scope parent by pk, or None (out-of-scope reads 404, no leak)."""
        raise NotImplementedError

    def profile_for(self, user) -> ParentProfile | None:
        """The signed-in user's own parent profile (self-service), or None."""
        raise NotImplementedError

    def students_for(self, parent: ParentProfile) -> QuerySet:
        """The parent's linked students (the sanctioned parents→students link)."""
        raise NotImplementedError


class IGuardianRepository(IBaseRepository[Guardian]):
    def scoped(self, *, user, roles) -> QuerySet[Guardian]:
        raise NotImplementedError

    def get_scoped(self, *, user, roles, pk: int) -> Guardian | None:
        raise NotImplementedError


class IPickupRepository(IBaseRepository[PickupAuthorization]):
    def scoped(self, *, user, roles) -> QuerySet[PickupAuthorization]:
        raise NotImplementedError

    def get_scoped(self, *, user, roles, pk: int) -> PickupAuthorization | None:
        raise NotImplementedError
