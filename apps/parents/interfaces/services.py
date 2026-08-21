"""Parent-domain service ports — one per aggregate (parent / guardian / pickup),
mirroring the three old viewsets. The views resolve these from the container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.parents.dto.parent_dto import GuardianCreateDTO, ParentCreateDTO, PickupCreateDTO
from apps.parents.models import Guardian, ParentProfile, PickupAuthorization


class IParentService(ABC):
    @abstractmethod
    def scoped_list(self, *, user, roles, permission: str) -> QuerySet[ParentProfile]: ...

    @abstractmethod
    def get(self, *, user, roles, permission: str, pk: int) -> ParentProfile | None: ...

    @abstractmethod
    def create(self, data: ParentCreateDTO, *, user, roles) -> ParentProfile: ...

    @abstractmethod
    def update(self, parent: ParentProfile, changes: dict[str, Any]) -> ParentProfile: ...

    @abstractmethod
    def deactivate(self, parent: ParentProfile, *, actor) -> ParentProfile: ...

    @abstractmethod
    def issue_credentials(self, parent: ParentProfile, *, actor) -> dict[str, Any]: ...

    @abstractmethod
    def students(
        self,
        parent: ParentProfile,
        *,
        user=None,
        roles=None,
        permission: str = "parents:read",
    ) -> QuerySet: ...

    @abstractmethod
    def assert_manage_scope(
        self,
        parent: ParentProfile,
        *,
        user,
        roles,
        permission: str,
    ) -> None: ...

    @abstractmethod
    def scope_allows(self, parent: ParentProfile, *, user, roles, permission: str) -> bool: ...

    # --- parent self-service (no parents:read grant; returns only own rows) ---
    @abstractmethod
    def require_profile(self, user) -> ParentProfile:
        """The caller's own parent profile, or raise 404 not_a_parent."""

    @abstractmethod
    def child_or_404(self, parent: ParentProfile, student_id: int):
        """One of the parent's linked children by id, or raise 404 not_your_child."""


class IGuardianService(ABC):
    @abstractmethod
    def scoped_list(self, *, user, roles, permission: str) -> QuerySet[Guardian]: ...

    @abstractmethod
    def get(self, *, user, roles, permission: str, pk: int) -> Guardian | None: ...

    @abstractmethod
    def create(self, data: GuardianCreateDTO, *, user, roles) -> Guardian: ...

    @abstractmethod
    def revoke(self, guardian: Guardian, *, user, roles, actor) -> Guardian: ...


class IPickupService(ABC):
    @abstractmethod
    def scoped_list(self, *, user, roles, permission: str) -> QuerySet[PickupAuthorization]: ...

    @abstractmethod
    def get(self, *, user, roles, permission: str, pk: int) -> PickupAuthorization | None: ...

    @abstractmethod
    def create(self, data: PickupCreateDTO, *, user, roles) -> PickupAuthorization: ...

    @abstractmethod
    def update(
        self,
        pickup: PickupAuthorization,
        changes: dict[str, Any],
        *,
        user,
        roles,
        actor,
    ) -> PickupAuthorization: ...

    @abstractmethod
    def deactivate(
        self,
        pickup: PickupAuthorization,
        *,
        user,
        roles,
        actor,
    ) -> PickupAuthorization: ...
