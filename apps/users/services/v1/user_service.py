"""User service — directory reads, self-service profile update, and the
self-scoped device registry. The identity/device domain functions
(register_device et al.) stay VERBATIM in ``apps.users.services`` (the package
__init__), which is imported by auth/students/teachers/parents.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from apps.users import services as users_domain
from apps.users.interfaces.repositories import IDeviceRepository, IUserRepository
from apps.users.interfaces.services import IUserService
from apps.users.models import Device, User
from core.exceptions import ValidationException


class UserService(IUserService):
    def __init__(self, user_repository: IUserRepository, device_repository: IDeviceRepository) -> None:
        self._users = user_repository
        self._devices = device_repository

    # --- directory ---
    def query(self) -> QuerySet[User]:
        return self._users.query()

    def get(self, pk: int) -> User | None:
        return self._users.get(pk)

    # --- self-service ---
    def update_me(self, *, user: User, changes: dict[str, Any]) -> User:
        # Reproduce the old DRF UniqueValidator on phone/email: a field-specific 400
        # (not the DB IntegrityError -> generic 409) when the value belongs to
        # another user. Best-effort like DRF's own validator (a concurrent write can
        # still hit the DB constraint -> 409, which is the correct backstop).
        self._reject_taken_identifier(user, changes)
        for field, value in changes.items():
            setattr(user, field, value)
        if changes:
            user.save(update_fields=list(changes.keys()))
        return user

    @staticmethod
    def _reject_taken_identifier(user: User, changes: dict[str, Any]) -> None:
        for field in ("phone", "email"):
            value = changes.get(field)
            if value and User.objects.filter(**{field: value}).exclude(pk=user.pk).exists():
                raise ValidationException(
                    "Invalid input.",
                    code="validation_error",
                    fields={field: [f"user with this {field} already exists."]},
                )

    # --- devices (self-scoped) ---
    def devices_for(self, user: User) -> QuerySet[Device]:
        return self._devices.active_for_user(user)

    def register_device(
        self, *, user: User, device_id: str, platform: str, user_agent: str, push_token: str
    ) -> Device | None:
        return users_domain.register_device(
            user=user,
            device_id=device_id,
            platform=platform,
            user_agent=user_agent,
            push_token=push_token,
        )

    def revoke_device(self, *, user: User, pk: int) -> bool:
        device = self._devices.get_for_user(user, pk)
        if device is None:
            return False
        device.revoked_at = timezone.now()
        device.save(update_fields=["revoked_at"])
        return True
