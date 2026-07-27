"""Service port for the users app."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.users.models import Device, User


class IUserService(ABC):
    # --- directory ---
    @abstractmethod
    def query(self) -> QuerySet[User]: ...

    @abstractmethod
    def get(self, pk: int) -> User | None: ...

    # --- self-service ---
    @abstractmethod
    def update_me(self, *, user: User, changes: dict[str, Any]) -> User: ...

    # --- devices (self-scoped) ---
    @abstractmethod
    def devices_for(self, user: User) -> QuerySet[Device]: ...

    @abstractmethod
    def register_device(
        self, *, user: User, device_id: str, platform: str, user_agent: str, push_token: str
    ) -> Device | None: ...

    @abstractmethod
    def revoke_device(self, *, user: User, pk: int) -> bool:
        """Soft-delete the caller's device. Returns False if it does not exist."""
