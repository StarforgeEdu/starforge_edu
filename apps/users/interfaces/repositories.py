"""Repository ports for the users app."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.users.models import Device, User


class IUserRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[User]:
        """Directory queryset (role_memberships prefetched)."""

    @abstractmethod
    def get(self, pk: int) -> User | None: ...


class IDeviceRepository(ABC):
    @abstractmethod
    def active_for_user(self, user: User) -> QuerySet[Device]:
        """The caller's own non-revoked devices."""

    @abstractmethod
    def get_for_user(self, user: User, pk: int) -> Device | None:
        """One of the caller's own non-revoked devices, or None."""
