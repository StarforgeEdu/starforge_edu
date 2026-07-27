"""AccessService — the layered facade over the A-2 permission-override CRUD.

Reproduces the old RolePermissionOverrideSerializer validation (valid role, a
well-formed non-wildcard non-`access` permission, a valid effect, no duplicate) as
clean 400s so a bad override can never 500 or slip past the anti-fraud invariants.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.access.dto.access_dto import OverrideDTO
from apps.access.interfaces.repositories import IOverrideRepository
from apps.access.interfaces.services import IAccessService
from apps.access.models import RolePermissionOverride
from apps.access.validation import validate_effect, validate_permission, validate_role
from core.exceptions import ValidationException


class AccessService(IAccessService):
    def __init__(self, overrides: IOverrideRepository) -> None:
        self._overrides = overrides

    def list_overrides(self) -> QuerySet[RolePermissionOverride]:
        return self._overrides.get_queryset()

    def get_override(self, pk: int) -> RolePermissionOverride | None:
        return self._overrides.get_by_id(pk)

    def create_override(self, data: OverrideDTO, *, actor) -> RolePermissionOverride:
        role = validate_role(data.role)
        permission = validate_permission(data.permission)
        effect = validate_effect(data.effect)
        if RolePermissionOverride.objects.filter(role=role, permission=permission).exists():
            # Friendly 400 instead of a unique-constraint 409/500 on duplicate create.
            raise ValidationException(
                _("An override for this role + permission already exists; update it instead."),
                code="validation_error",
            )
        return RolePermissionOverride.objects.create(
            role=role, permission=permission, effect=effect, note=data.note, created_by=actor
        )

    def update_override(
        self, override: RolePermissionOverride, changes: dict[str, Any]
    ) -> RolePermissionOverride:
        if "role" in changes:
            override.role = validate_role(changes["role"])
        if "permission" in changes:
            override.permission = validate_permission(changes["permission"])
        if "effect" in changes:
            override.effect = validate_effect(changes["effect"])
        if "note" in changes:
            override.note = changes["note"]
        override.save()  # a dup (role, permission) surfaces as a 409 via the middleware
        return override

    def delete_override(self, override: RolePermissionOverride) -> None:
        override.delete()
