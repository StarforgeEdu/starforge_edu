"""User + Device repositories — the ORM touchpoints for the users read side."""

from __future__ import annotations

from django.db.models import Prefetch, QuerySet

from apps.users.interfaces.repositories import IDeviceRepository, IUserRepository
from apps.users.models import Device, RoleMembership, User
from core.repositories import BaseRepository


class UserRepository(BaseRepository[User], IUserRepository):
    model = User

    def query(self) -> QuerySet[User]:
        return User.objects.prefetch_related(
            Prefetch(
                "role_memberships",
                queryset=RoleMembership.objects.select_related("account_type", "branch", "department"),
            )
        ).all()

    def get(self, pk: int) -> User | None:
        return self.query().filter(pk=pk).first()


class DeviceRepository(BaseRepository[Device], IDeviceRepository):
    model = Device

    def active_for_user(self, user: User) -> QuerySet[Device]:
        return Device.objects.filter(user=user, revoked_at__isnull=True)

    def get_for_user(self, user: User, pk: int) -> Device | None:
        return self.active_for_user(user).filter(pk=pk).first()
