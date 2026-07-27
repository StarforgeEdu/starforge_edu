"""The auth service port — the contract the views depend on (resolved from the
container). The implementation is ``apps.auth.services.v1.auth_service.AuthService``."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from apps.auth.dto.auth_dto import (
    ChangePasswordDTO,
    LoginDTO,
    ResetConfirmDTO,
    ResetRequestDTO,
    SessionContextDTO,
)
from apps.users.models import User


class IAuthService(ABC):
    @abstractmethod
    def login(self, credentials: LoginDTO, ctx: SessionContextDTO) -> dict[str, str]:
        """Authenticate username+password; on success register the device and return
        ``{"access": <session key>}``. Failures raise AuthenticationException
        (invalid_credentials) — unknown user, wrong password, and inactive account are
        indistinguishable."""

    @abstractmethod
    def role_login(self, credentials: LoginDTO, ctx: SessionContextDTO) -> dict[str, Any]:
        """Role-native login: authenticate a role account (student/teacher/parent/staff) by
        its OWN username+password; returns ``{access, role, must_change_password}``. Same
        indistinguishable-failure contract as ``login``."""

    @abstractmethod
    def logout(self, user: User) -> None:
        """Revoke every session for the caller (instant server-side logout)."""

    @abstractmethod
    def change_password(
        self,
        user: User,
        data: ChangePasswordDTO,
        *,
        principal_kind: str = "",
        principal_id: int | None = None,
    ) -> dict[str, str]:
        """Verify the old password, set the new one (revoking all sessions), and return
        a fresh session so the current device stays logged in."""

    @abstractmethod
    def request_reset(self, data: ResetRequestDTO, ctx: SessionContextDTO) -> None:
        """Send a reset code IF an account matches — silent otherwise (anti-enumeration)."""

    @abstractmethod
    def confirm_reset(self, data: ResetConfirmDTO, ctx: SessionContextDTO) -> None:
        """Verify the reset code and set the new password (ends every session)."""
