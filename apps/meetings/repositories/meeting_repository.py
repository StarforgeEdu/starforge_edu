"""ORM-backed staff-meeting repository (role-scoped reads)."""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.meetings.interfaces.repositories import IMeetingRepository
from apps.meetings.models import StaffMeeting
from core.repositories import BaseRepository


class MeetingRepository(BaseRepository[StaffMeeting], IMeetingRepository):
    model = StaffMeeting

    def get_queryset(self) -> QuerySet[StaffMeeting]:
        return StaffMeeting.objects.select_related("branch", "created_by", "cancelled_by").prefetch_related(
            "attendees"
        )

    def scoped(self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int]) -> QuerySet[StaffMeeting]:
        qs = self.get_queryset()
        if is_unscoped:
            return qs
        if is_manager:
            # Branch meetings union ones they were personally invited to (so a cross-branch
            # invite they see in /upcoming/ can also be opened + RSVP'd).
            return qs.filter(Q(branch_id__in=branch_ids) | Q(attendees__user=user)).distinct()
        return qs.filter(attendees__user=user).distinct()  # invitees see only their own

    def get_scoped(
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int], pk: int
    ) -> StaffMeeting | None:
        return (
            self.scoped(user=user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids)
            .filter(pk=pk)
            .first()
        )

    def upcoming_for(self, user) -> QuerySet[StaffMeeting]:
        return (
            self.get_queryset()
            .filter(
                attendees__user=user,
                status=StaffMeeting.Status.SCHEDULED,
                starts_at__gte=timezone.now(),
            )
            .order_by("starts_at")
            .distinct()
        )
