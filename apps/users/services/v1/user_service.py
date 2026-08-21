"""User service — directory reads, self-service profile update, and the
self-scoped device registry. The identity/device domain functions
(register_device et al.) stay VERBATIM in ``apps.users.services`` (the package
__init__), which is imported by auth/students/teachers/parents.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.auth.interfaces.repositories import ISessionRepository
from apps.users import services as users_domain
from apps.users.interfaces.repositories import IDeviceRepository, IUserRepository
from apps.users.interfaces.services import IUserService
from apps.users.models import Device, Session, User
from core.exceptions import ValidationException


class UserService(IUserService):
    def __init__(
        self,
        user_repository: IUserRepository,
        device_repository: IDeviceRepository,
        session_repository: ISessionRepository,
    ) -> None:
        self._users = user_repository
        self._devices = device_repository
        self._sessions = session_repository

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
                    _("Invalid input."),
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
        # The provider token is a credential-like endpoint. Erase it on revoke
        # so a shared or signed-out installation cannot be targeted again.
        device.push_token = ""
        device.save(update_fields=["revoked_at", "push_token"])
        return True

    # --- sessions (self-scoped to the exact authenticated principal) ---
    def sessions_for(
        self,
        *,
        user: User,
        principal_kind: str,
        principal_id: int | None,
    ) -> QuerySet[Session]:
        return self._sessions.active_for_principal(
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )

    @transaction.atomic
    def revoke_session(
        self,
        *,
        user: User,
        principal_kind: str,
        principal_id: int | None,
        session_id: int,
    ) -> bool:
        changed = self._sessions.revoke_for_principal(
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
            session_id=session_id,
        )
        if not changed:
            return False
        from apps.audit.services import audit_log

        audit_log(
            actor=user,
            action="session.revoked",
            resource_type="users.Session",
            resource_id=str(session_id),
        )
        return True
