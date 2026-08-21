"""ORM-backed permission-override repository (A-2)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.access.interfaces.repositories import IOverrideRepository
from apps.access.models import RolePermissionOverride
from core.repositories import BaseRepository


class OverrideRepository(BaseRepository[RolePermissionOverride], IOverrideRepository):
    model = RolePermissionOverride

    def get_queryset(self) -> QuerySet[RolePermissionOverride]:
        return RolePermissionOverride.objects.select_related("created_by")
