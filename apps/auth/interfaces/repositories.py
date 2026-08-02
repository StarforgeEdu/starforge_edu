"""Repository ports for the auth domain (data-access contracts the service depends
on). Concrete ORM implementations live in ``apps.auth.repositories`` and are bound
to these ports in ``apps.auth.apps.AuthAppConfig.ready``."""

from __future__ import annotations

from abc import abstractmethod

from django.db.models import QuerySet

from apps.users.models import Session, User
from core.interfaces import IBaseRepository


class IUserRepository(IBaseRepository[User]):
    @abstractmethod
    def get_by_username(self, username: str) -> User | None: ...

    @abstractmethod
    def find_by_identifier(self, identifier: str) -> User | None:
        """Resolve a phone (E.164) or email to its user — for password reset."""

    @abstractmethod
    def touch_last_seen(self, user: User) -> None: ...


class ISessionRepository(IBaseRepository[Session]):
    @abstractmethod
    def create_for(
        self,
        user: User,
        *,
        ip: str = "",
        user_agent: str = "",
        device_id: str = "",
        principal_kind: str = "",
        principal_id: int | None = None,
    ) -> Session: ...

    @abstractmethod
    def revoke(self, session_id: int) -> int:
        """Revoke one active session. Returns the number of rows changed."""

    @abstractmethod
    def revoke_all_for_user(self, user_id: int) -> int: ...

    @abstractmethod
    def active_for_principal(
        self,
        *,
        user: User,
        principal_kind: str,
        principal_id: int | None,
    ) -> QuerySet[Session]:
        """Return only live sessions for one exact role-native principal."""

    @abstractmethod
    def revoke_for_principal(
        self,
        *,
        user: User,
        principal_kind: str,
        principal_id: int | None,
        session_id: int,
    ) -> int:
        """Revoke one visible live session, returning the changed row count."""
