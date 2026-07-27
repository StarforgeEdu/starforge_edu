"""ORM-backed session repository (auth domain). Session creation/revocation has a
single source of truth in ``core.session_auth`` (the same helpers the authenticator
uses); this repository is the auth domain's injected interface to it."""

from __future__ import annotations

from apps.auth.interfaces.repositories import ISessionRepository
from apps.users.models import Session, User
from core.repositories import BaseRepository


class SessionRepository(BaseRepository[Session], ISessionRepository):
    model = Session

    def create_for(
        self,
        user: User,
        *,
        ip: str = "",
        user_agent: str = "",
        device_id: str = "",
        principal_kind: str = "",
        principal_id: int | None = None,
    ) -> Session:
        from core.session_auth import create_session

        return create_session(
            user,
            ip=ip,
            user_agent=user_agent,
            device_id=device_id,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    def revoke_all_for_user(self, user_id: int) -> int:
        from core.session_auth import revoke_all_for_user

        return revoke_all_for_user(user_id)
