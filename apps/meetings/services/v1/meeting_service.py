"""MeetingService — schedule / cancel / RSVP + role-scoped reads. Reuses the tested
domain fns (schedule_meeting / cancel_meeting / respond_to_meeting) unchanged."""

from __future__ import annotations

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.meetings.dto.meeting_dto import ScheduleMeetingDTO
from apps.meetings.interfaces.repositories import IMeetingRepository
from apps.meetings.interfaces.services import IMeetingService
from apps.meetings.models import MeetingAttendee, StaffMeeting
from core.exceptions import ValidationException


class MeetingService(IMeetingService):
    def __init__(self, meetings: IMeetingRepository) -> None:
        self._meetings = meetings

    def scoped_list(self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int]) -> QuerySet[StaffMeeting]:
        return self._meetings.scoped(
            user=user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids
        )

    def get_visible(
        self, *, user, is_unscoped: bool, is_manager: bool, branch_ids: set[int], pk: int
    ) -> StaffMeeting | None:
        return self._meetings.get_scoped(
            user=user, is_unscoped=is_unscoped, is_manager=is_manager, branch_ids=branch_ids, pk=pk
        )

    def upcoming_for(self, user) -> QuerySet[StaffMeeting]:
        return self._meetings.upcoming_for(user)

    def schedule(self, data: ScheduleMeetingDTO, *, created_by) -> StaffMeeting:
        from apps.meetings.services import schedule_meeting

        return schedule_meeting(
            title=data.title,
            agenda=data.agenda,
            starts_at=data.starts_at,
            ends_at=data.ends_at,
            location=data.location,
            attendees=self._resolve_attendees(data.attendee_ids),
            created_by=created_by,
            branch=self.resolve_branch(data.branch_id),
        )

    def cancel(self, meeting: StaffMeeting, *, actor) -> StaffMeeting:
        from apps.meetings.services import cancel_meeting

        return cancel_meeting(meeting_id=meeting.pk, actor=actor)

    def respond(self, meeting: StaffMeeting, *, user, response: str) -> MeetingAttendee:
        from apps.meetings.services import respond_to_meeting

        return respond_to_meeting(meeting_id=meeting.pk, user=user, response=response)

    def resolve_branch(self, branch_id: int | None):
        if branch_id is None:
            return None
        from apps.org.models import Branch

        # Archived branches are not assignable (mirrors the old serializer queryset).
        branch = Branch.objects.filter(pk=branch_id, archived_at__isnull=True).first()
        if branch is None:
            raise ValidationException(
                _("Invalid branch."), code="invalid_branch", fields={"branch": ["Not found."]}
            )
        return branch

    @staticmethod
    def _resolve_attendees(ids: list[int]) -> list:
        from apps.users.models import User
        from core.permissions import Role

        if not ids:
            raise ValidationException(
                _("Invite at least one attendee."),
                code="validation_error",
                fields={"attendees": ["This list may not be empty."]},
            )
        # Meetings are staff coordination — invitees must be active staff (not students/
        # parents), mirroring the old ScheduleMeetingSerializer attendees queryset.
        staff_roles = tuple(r for r in Role.ALL if r not in (Role.STUDENT, Role.PARENT))
        deduped = list(dict.fromkeys(ids))
        users = list(
            User.objects.filter(
                pk__in=deduped,
                is_active=True,
                role_memberships__revoked_at__isnull=True,
                role_memberships__role__in=staff_roles,
            ).distinct()
        )
        if len(users) != len(deduped):
            raise ValidationException(
                _("One or more attendees are not valid staff in this center."),
                code="validation_error",
                fields={"attendees": ["One or more are not valid staff recipients."]},
            )
        return users
