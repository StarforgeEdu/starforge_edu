"""ORM-backed staff-meeting repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone

from apps.meetings.interfaces.repositories import IMeetingRepository
from apps.meetings.models import MeetingAttendee, StaffMeeting
from core.repositories import BaseRepository


class MeetingRepository(BaseRepository[StaffMeeting], IMeetingRepository):
    model = StaffMeeting

    def get_queryset(self) -> QuerySet[StaffMeeting]:
        return StaffMeeting.objects.select_related(
            "branch",
            "created_by",
            "created_by__staff_profile",
            "created_by__teacher_profile",
            "cancelled_by",
            "cancelled_by__staff_profile",
            "cancelled_by__teacher_profile",
        ).prefetch_related(
            Prefetch(
                "attendees",
                queryset=MeetingAttendee.objects.select_related(
                    "user",
                    "user__staff_profile",
                    "user__teacher_profile",
                ),
            )
        )

    def scoped(
        self,
        *,
        is_unscoped: bool,
        is_manager: bool,
        branch_ids: set[int],
        principal_kind: str,
        principal_id: int,
    ) -> QuerySet[StaffMeeting]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        if is_manager:
            # Branch meetings union ones they were personally invited to (so a cross-branch
            # invite they see in /upcoming/ can also be opened + RSVP'd).
            return qs.filter(
                Q(branch_id__in=branch_ids)
                | Q(attendees__principal_kind=principal_kind, attendees__principal_id=principal_id)
            ).distinct()
        return qs.filter(
            attendees__principal_kind=principal_kind,
            attendees__principal_id=principal_id,
        ).distinct()

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
        return (
            self.scoped(
                is_unscoped=is_unscoped,
                is_manager=is_manager,
                branch_ids=branch_ids,
                principal_kind=principal_kind,
                principal_id=principal_id,
            )
            .filter(pk=pk)
            .first()
        )

    def upcoming_for(self, *, principal_kind: str, principal_id: int) -> QuerySet[StaffMeeting]:
        return (
            self.get_queryset()
            .filter(
                attendees__principal_kind=principal_kind,
                attendees__principal_id=principal_id,
                status=StaffMeeting.Status.SCHEDULED,
                starts_at__gte=timezone.now(),
            )
            .order_by("starts_at")
            .distinct()
        )
