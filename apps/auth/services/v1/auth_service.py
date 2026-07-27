"""AuthService — the IAuthService implementation.

Orchestration only: it depends on the repository PORTS (injected by the container),
not the ORM, and reuses the tested domain helpers in ``apps.auth.services`` for the
security-sensitive bits (timing-equalized login failures, password validation, the
anti-enumeration OTP reset flow). Data access goes through the repositories.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth.hashers import check_password
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
from apps.users.models import User
from core.exceptions import AuthenticationException, ValidationException


class AuthService(IAuthService):
    def __init__(self, users: IUserRepository, sessions: ISessionRepository) -> None:
        self._users = users
        self._sessions = sessions

    def login(self, credentials: LoginDTO, ctx: SessionContextDTO) -> dict[str, str]:
        from apps.auth.services import _dummy_hash, _fire_login_failed
        from apps.auth.signals import login_succeeded
        from apps.users.services import register_device
        from core.utils import current_schema

        username = credentials.username.strip()
        user = self._users.get_by_username(username)
        # Unknown user, wrong password, and inactive account are indistinguishable to
        # the caller; a dummy hash check keeps the unknown-user path timing-equivalent.
        if user is None:
            check_password(credentials.password, _dummy_hash())
            _fire_login_failed(username, ctx.ip, ctx.user_agent, reason="unknown_username")
            raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")
        if not user.check_password(credentials.password) or not user.is_active:
            reason = "wrong_password" if user.is_active else "inactive_user"
            _fire_login_failed(username, ctx.ip, ctx.user_agent, reason=reason)
            raise AuthenticationException(_("Invalid username or password."), code="invalid_credentials")

        self._users.touch_last_seen(user)
        login_succeeded.send(
            sender=User,
            username=username,
            user_id=user.pk,
            ip=ctx.ip,
            user_agent=ctx.user_agent,
            schema_name=current_schema(),
        )
        register_device(
            user=user,
            device_id=credentials.device_id,
            platform=credentials.platform,
            user_agent=ctx.user_agent,
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
        )

    def logout(self, user: User) -> None:
        # Revoke the signed iCal credential (token_version) together with every
        # opaque session, using the domain's single logout path.
        from apps.auth.services import logout_everywhere

        logout_everywhere(user)

    def change_password(
        self,
        user: User,
        data: ChangePasswordDTO,
        *,
        principal_kind: str = "",
        principal_id: int | None = None,
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
                raise ValidationException(_("Current password is incorrect."), code="wrong_password")
            _validate_new_password(data.new_password, account)
            set_role_account_password(account, data.new_password, must_change=False)
            session = self._sessions.create_for(
                user,
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
            return {"access": session.key}

        if not user.check_password(data.old_password):
            raise ValidationException(_("Current password is incorrect."), code="wrong_password")
        _validate_new_password(data.new_password, user)
        set_user_password(user, data.new_password)  # revokes every session for the user
        session = self._sessions.create_for(user)  # fresh session for THIS device
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
