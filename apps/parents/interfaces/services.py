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
    def scoped_list(self, *, user, roles) -> QuerySet[ParentProfile]: ...

    @abstractmethod
    def get(self, *, user, roles, pk: int) -> ParentProfile | None: ...

    @abstractmethod
    def create(self, data: ParentCreateDTO) -> ParentProfile: ...

    @abstractmethod
    def update(self, parent: ParentProfile, changes: dict[str, Any]) -> ParentProfile: ...

    @abstractmethod
    def delete(self, parent: ParentProfile) -> None: ...

    @abstractmethod
    def students(self, parent: ParentProfile) -> QuerySet: ...

    # --- parent self-service (no parents:read grant; returns only own rows) ---
    @abstractmethod
    def require_profile(self, user) -> ParentProfile:
        """The caller's own parent profile, or raise 404 not_a_parent."""

    @abstractmethod
    def child_or_404(self, parent: ParentProfile, student_id: int):
        """One of the parent's linked children by id, or raise 404 not_your_child."""


class IGuardianService(ABC):
    @abstractmethod
    def scoped_list(self, *, user, roles) -> QuerySet[Guardian]: ...

    @abstractmethod
    def get(self, *, user, roles, pk: int) -> Guardian | None: ...

    @abstractmethod
    def create(self, data: GuardianCreateDTO) -> Guardian: ...

    @abstractmethod
    def delete(self, guardian: Guardian) -> None: ...


class IPickupService(ABC):
    @abstractmethod
    def scoped_list(self, *, user, roles) -> QuerySet[PickupAuthorization]: ...

    @abstractmethod
    def get(self, *, user, roles, pk: int) -> PickupAuthorization | None: ...

    @abstractmethod
    def create(self, data: PickupCreateDTO) -> PickupAuthorization: ...

    @abstractmethod
    def update(self, pickup: PickupAuthorization, changes: dict[str, Any]) -> PickupAuthorization: ...

    @abstractmethod
    def delete(self, pickup: PickupAuthorization) -> None: ...
