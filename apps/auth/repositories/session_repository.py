"""ORM-backed session repository (auth domain). Session creation/revocation has a
single source of truth in ``core.session_auth`` (the same helpers the authenticator
uses); this repository is the auth domain's injected interface to it."""

from __future__ import annotations

from django.db.models import OuterRef, QuerySet, Subquery
from django.utils import timezone

from apps.auth.interfaces.repositories import ISessionRepository
from apps.users.models import Device, Session, User
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

    def revoke(self, session_id: int) -> int:
        return self.model.objects.filter(pk=session_id, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )

    def active_for_principal(
        self,
        *,
        user: User,
        principal_kind: str,
        principal_id: int | None,
    ) -> QuerySet[Session]:
        from core.session_auth import session_idle_timeout

        now = timezone.now()
        queryset = self.model.objects.filter(
            user=user,
            revoked_at__isnull=True,
            expires_at__gt=now,
            last_used_at__gt=now - session_idle_timeout(),
        )
        if principal_kind:
            queryset = queryset.filter(
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
        else:
            queryset = queryset.filter(principal_kind="", principal_id__isnull=True)

        active_device = Device.objects.filter(
            user_id=OuterRef("user_id"),
            device_id=OuterRef("device_id"),
            revoked_at__isnull=True,
        ).order_by("-last_seen_at")
        return queryset.annotate(
            device_platform=Subquery(active_device.values("platform")[:1]),
        )

    def revoke_for_principal(
        self,
        *,
        user: User,
        principal_kind: str,
        principal_id: int | None,
        session_id: int,
    ) -> int:
        return (
            self.active_for_principal(
                user=user,
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
            .filter(pk=session_id)
            .update(revoked_at=timezone.now())
        )
