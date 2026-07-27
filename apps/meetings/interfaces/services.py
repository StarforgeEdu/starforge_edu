"""Meeting service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.meetings.dto.meeting_dto import ScheduleMeetingDTO
from apps.meetings.models import MeetingAttendee, StaffMeeting


class IMeetingService(ABC):
    @abstractmethod
    def scoped_list(self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int]) -> QuerySet[StaffMeeting]: ...

    @abstractmethod
    def get_visible(
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int], pk: int
    ) -> StaffMeeting | None: ...

    @abstractmethod
    def upcoming_for(self, user) -> QuerySet[StaffMeeting]: ...

    @abstractmethod
    def schedule(self, data: ScheduleMeetingDTO, *, created_by) -> StaffMeeting: ...

    @abstractmethod
    def cancel(self, meeting: StaffMeeting, *, actor) -> StaffMeeting: ...

    @abstractmethod
    def respond(self, meeting: StaffMeeting, *, user, response: str) -> MeetingAttendee: ...

    @abstractmethod
    def resolve_branch(self, branch_id: int | None):
        """Resolve an active branch by id (400 if archived/missing), or None."""
