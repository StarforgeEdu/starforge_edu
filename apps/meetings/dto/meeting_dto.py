"""Meeting-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from apps.users.models import User


@dataclass(frozen=True)
class ScheduleMeetingDTO:
    title: str
    starts_at: datetime
    ends_at: datetime
    attendee_ids: list[int]
    invitee_principals: list[MeetingPrincipalTarget] | None = None
    agenda: str = ""
    location: str = ""
    branch_id: int | None = None


@dataclass(frozen=True)
class MeetingInvitee:
    user: User
    principal_kind: str
    principal_id: int


@dataclass(frozen=True)
class MeetingPrincipalTarget:
    """Role-native invite selector used when a bridge User has multiple accounts."""

    principal_kind: str
    principal_id: int
