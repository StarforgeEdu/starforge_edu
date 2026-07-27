"""ORM-backed user repository (auth domain) — the only auth code that queries User."""

from __future__ import annotations

from django.utils import timezone

from apps.auth.interfaces.repositories import IUserRepository
from apps.users.models import User
from core.repositories import BaseRepository


class UserRepository(BaseRepository[User], IUserRepository):
    model = User

    def get_by_username(self, username: str) -> User | None:
        return self.get_queryset().filter(username=username).first()

    def find_by_identifier(self, identifier: str) -> User | None:
        # The identifier arrives already normalized (E.164 phone or lowercased email).
        lookup = {"email": identifier} if "@" in identifier else {"phone": identifier}
        return self.get_queryset().filter(**lookup).first()

    def touch_last_seen(self, user: User) -> None:
        user.last_seen_at = timezone.now()
        user.save(update_fields=["last_seen_at"])
