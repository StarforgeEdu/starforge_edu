"""AuthService — the IAuthService implementation.

Orchestration only: it depends on the repository PORTS (injected by the container),
not the ORM, and reuses the tested domain helpers in ``apps.auth.services`` for the
security-sensitive bits (timing-equalized login failures, password validation, the
anti-enumeration OTP reset flow). Data access goes through the repositories.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.auth.dto.auth_dto import (
    ChangePasswordDTO,
    LoginDTO,
    ResetConfirmDTO,
    ResetRequestDTO,
    SessionContextDTO,
)
from apps.auth.interfaces.auth_service import IAuthService
from apps.auth.interfaces.repositories import ISessionRepository, IUserRepository
from apps.users.models import Device, Session, User
from core.exceptions import AuthenticationException, ValidationException


class AuthService(IAuthService):
    def __init__(self, users: IUserRepository, sessions: ISessionRepository) -> None:
        self._users = users
        self._sessions = sessions

    def login(self, credentials: LoginDTO, ctx: SessionContextDTO) -> dict[str, str]:
        from django_tenants.utils import get_public_schema_name

        from apps.auth.services import _dummy_hash, _fire_login_failed
        from apps.auth.signals import login_succeeded
        from apps.users.services import register_device
        from core.exceptions import NotFoundException
        from core.utils import current_schema

        if current_schema() != get_public_schema_name():
            raise NotFoundException(code="not_found")

        username = credentials.username.strip()
        user = self._users.get_by_username(username)
        # Unknown user, wrong password, and inactive account are indistinguishable to
        # the caller; a dummy hash check keeps the unknown-user path timing-equivalent.
        if user is None:
            check_password(credentials.password, _dummy_hash())
            _fire_login_failed(username, ctx.ip, ctx.user_agent, reason="unknown_username")
            raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")
        if (
            not user.check_password(credentials.password)
            or not user.is_active
            or not (user.is_staff or user.is_superuser)
        ):
            reason = "wrong_password" if user.is_active else "inactive_user"
            _fire_login_failed(username, ctx.ip, ctx.user_agent, reason=reason)
            raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")

        self._users.touch_last_seen(user)
        normalized_device_id = credentials.device_id[:128]
        was_known_device = bool(
            normalized_device_id
            and credentials.platform
            and Device.objects.filter(
                user=user,
                device_id=normalized_device_id,
                revoked_at__isnull=True,
            ).exists()
        )
        device = register_device(
            user=user,
            device_id=normalized_device_id,
            platform=credentials.platform,
            user_agent=ctx.user_agent,
        )
        login_succeeded.send(
            sender=User,
            username=username,
            user_id=user.pk,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            device_id=device.device_id if device is not None else "",
            is_new_device=device is not None and not was_known_device,
            schema_name=current_schema(),
        )
        session = self._sessions.create_for(
            user, ip=ctx.ip, user_agent=ctx.user_agent, device_id=credentials.device_id
        )
        return {"access": session.key}

    def role_login(self, credentials: LoginDTO, ctx: SessionContextDTO) -> dict[str, Any]:
        from apps.auth.services import role_login as _role_login

        return _role_login(
            username=credentials.username,
            password=credentials.password,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            device_id=credentials.device_id,
            platform=credentials.platform,
        )

    def logout(self, session: Session) -> None:
        """Revoke only the credential used by this request."""

        if not self._sessions.revoke(session.pk):
            return
        from apps.audit.services import audit_log

        audit_log(
            actor=session.user,
            action="logout",
            resource_type="users.Session",
            resource_id=str(session.pk),
        )

    def logout_all(self, user: User) -> None:
        from apps.auth.services import logout_everywhere

        logout_everywhere(user)

    @transaction.atomic
    def change_password(
        self,
        user: User,
        data: ChangePasswordDTO,
        *,
        principal_kind: str = "",
        principal_id: int | None = None,
        device_id: str = "",
        ip: str = "",
        user_agent: str = "",
    ) -> dict[str, str]:
        from apps.auth.services import _validate_new_password
        from apps.users.services import set_role_account_password, set_user_password

        if principal_kind and principal_id is not None:
            from apps.auth.services import _role_account_models

            model = _role_account_models().get(principal_kind)
            account = model.objects.filter(pk=principal_id, user=user).first() if model else None
            if account is None:
                raise AuthenticationException(_("Invalid account session."), code="authentication_failed")
            if not account.check_password(data.old_password):
                raise ValidationException(
                    _("The current password is incorrect."),
                    code="wrong_password",
                    fields={"old_password": [_("The current password is incorrect.")]},
                )
            _validate_new_password(data.new_password, account)
            set_role_account_password(account, data.new_password, must_change=False)
            session = self._sessions.create_for(
                user,
                ip=ip,
                user_agent=user_agent,
                device_id=device_id,
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
            return {"access": session.key}

        if not user.check_password(data.old_password):
            raise ValidationException(
                _("The current password is incorrect."),
                code="wrong_password",
                fields={"old_password": [_("The current password is incorrect.")]},
            )
        _validate_new_password(data.new_password, user)
        current_device = None
        if device_id:
            current_device = (
                Device.objects.filter(
                    user=user,
                    device_id=device_id[:128],
                    revoked_at__isnull=True,
                )
                .values("device_id", "platform", "push_token", "user_agent")
                .first()
            )
        set_user_password(user, data.new_password)  # revokes every session for the user
        session = self._sessions.create_for(
            user,
            ip=ip,
            user_agent=user_agent,
            device_id=device_id,
        )
        if current_device is not None:
            from apps.users.services import register_device

            register_device(
                user=user,
                device_id=current_device["device_id"],
                platform=current_device["platform"],
                push_token=current_device["push_token"],
                user_agent=user_agent or current_device["user_agent"],
            )
        return {"access": session.key}

    def request_reset(self, data: ResetRequestDTO, ctx: SessionContextDTO) -> None:
        from apps.auth.services import request_password_reset

        request_password_reset(
            identifier=data.identifier,
            account_type=data.account_type,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
        )

    def confirm_reset(self, data: ResetConfirmDTO, ctx: SessionContextDTO) -> None:
        from apps.auth.services import reset_password

        reset_password(
            identifier=data.identifier,
            code=data.code,
            new_password=data.new_password,
            account_type=data.account_type,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
        )
