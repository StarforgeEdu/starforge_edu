"""Meeting service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.meetings.dto.meeting_dto import MeetingInvitee, MeetingPrincipalTarget, ScheduleMeetingDTO
from apps.meetings.models import MeetingAttendee, StaffMeeting


class IMeetingService(ABC):
    @abstractmethod
    def scoped_list(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
    ) -> QuerySet[StaffMeeting]: ...

    @abstractmethod
    def get_visible(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
        pk: int,
    ) -> StaffMeeting | None: ...

    @abstractmethod
    def upcoming_for(self, *, principal_kind: str, principal_id: int) -> QuerySet[StaffMeeting]: ...

    @abstractmethod
    def schedule(
        self,
        data: ScheduleMeetingDTO,
        *,
        created_by,
        created_by_principal_kind: str,
        created_by_principal_id: int,
        branch,
        attendees: list[MeetingInvitee],
    ) -> StaffMeeting: ...

    @abstractmethod
    def cancel(
        self,
        meeting: StaffMeeting,
        *,
        actor,
        actor_principal_kind: str,
        actor_principal_id: int,
    ) -> StaffMeeting: ...

    @abstractmethod
    def respond(
        self,
        meeting: StaffMeeting,
        *,
        user,
        principal_kind: str,
        principal_id: int,
        response: str,
    ) -> MeetingAttendee: ...

    @abstractmethod
    def resolve_branch(self, branch_id: int | None):
        """Resolve an active branch by id (400 if archived/missing), or None."""

    @abstractmethod
    def resolve_attendees(
        self,
        ids: list[int],
        *,
        principal_targets: list[MeetingPrincipalTarget] | None,
        branch_id: int | None,
    ) -> list[MeetingInvitee]:
        """Resolve active staff invitees or raise a field-level 400."""
