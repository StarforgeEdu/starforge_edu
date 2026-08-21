"""Access-config service port (A-2 permission overrides)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.access.dto.access_dto import OverrideDTO
from apps.access.models import RolePermissionOverride


class IAccessService(ABC):
    @abstractmethod
    def list_overrides(self) -> QuerySet[RolePermissionOverride]: ...

    @abstractmethod
    def get_override(self, pk: int) -> RolePermissionOverride | None: ...

    @abstractmethod
    def create_override(self, data: OverrideDTO, *, actor) -> RolePermissionOverride: ...

    @abstractmethod
    def update_override(
        self, override: RolePermissionOverride, changes: dict[str, Any]
    ) -> RolePermissionOverride: ...

    @abstractmethod
    def delete_override(self, override: RolePermissionOverride) -> None: ...
