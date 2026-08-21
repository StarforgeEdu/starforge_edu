"""Meeting repository port. Read scoping is role-based (superuser/DIRECTOR see all;
a manager sees their branch's meetings union ones they were invited to; anyone else only
the ones they were invited to)."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.meetings.models import StaffMeeting
from core.interfaces import IBaseRepository


class IMeetingRepository(IBaseRepository[StaffMeeting]):
    def scoped(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
    ) -> QuerySet[StaffMeeting]:
        raise NotImplementedError

    def get_scoped(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
        pk: int,
    ) -> StaffMeeting | None:
        raise NotImplementedError

    def upcoming_for(self, *, principal_kind: str, principal_id: int) -> QuerySet[StaffMeeting]:
        """The user's upcoming scheduled meetings (as an invitee)."""
        raise NotImplementedError
