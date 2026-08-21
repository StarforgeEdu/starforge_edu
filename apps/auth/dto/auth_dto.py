"""Auth DTOs — immutable inputs the views build from the request body and hand to
the service. Frozen dataclasses: no behaviour, just validated transport."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionContextDTO:
    """Request-derived context attached to a new session (audit / device binding)."""

    ip: str = ""
    user_agent: str = ""


@dataclass(frozen=True)
class LoginDTO:
    username: str
    password: str
    device_id: str = ""
    platform: str = ""


@dataclass(frozen=True)
class ChangePasswordDTO:
    old_password: str
    new_password: str


@dataclass(frozen=True)
class ResetRequestDTO:
    identifier: str  # phone or email on file
    account_type: str = ""


@dataclass(frozen=True)
class ResetConfirmDTO:
    identifier: str
    code: str
    new_password: str
    account_type: str = ""
