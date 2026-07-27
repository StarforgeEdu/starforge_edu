"""Staff-meeting services (F3-5): schedule (with invites), cancel, and RSVP."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.meetings.models import MeetingAttendee, StaffMeeting
from core.exceptions import NotFoundException, UnprocessableEntity, ValidationException


@transaction.atomic
def schedule_meeting(
    *, title, agenda="", starts_at, ends_at, location="", attendees, created_by, branch=None
) -> StaffMeeting:
    """Create a meeting and invite the given staff (each starts INVITED). Attendees are
    validated as active staff by the serializer."""
    if ends_at <= starts_at:
        raise ValidationException(_("A meeting must end after it starts."), code="meeting_ends_before_start")
    meeting = StaffMeeting.objects.create(
        title=title,
        agenda=agenda,
        branch=branch,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
        created_by=created_by,
    )
    # Dedupe the invitee list so a repeated user id can't trip the unique constraint.
    seen: set[int] = set()
    rows = []
    for user in attendees:
        if user.id in seen:
            continue
        seen.add(user.id)
        rows.append(MeetingAttendee(meeting=meeting, user=user))
    MeetingAttendee.objects.bulk_create(rows)
    return meeting


@transaction.atomic
def cancel_meeting(*, meeting_id: int, actor) -> StaffMeeting:
    meeting = StaffMeeting.objects.select_for_update().filter(pk=meeting_id).first()
    if meeting is None:
        raise NotFoundException(_("Meeting not found."), code="meeting_not_found")
    if meeting.status != StaffMeeting.Status.SCHEDULED:
        raise UnprocessableEntity(
            _("Only a scheduled meeting can be cancelled."), code="meeting_not_scheduled"
        )
    meeting.status = StaffMeeting.Status.CANCELLED
    meeting.cancelled_by = actor
    meeting.cancelled_at = timezone.now()
    meeting.save(update_fields=["status", "cancelled_by", "cancelled_at"])
    return meeting


@transaction.atomic
def respond_to_meeting(*, meeting_id: int, user, response: str) -> MeetingAttendee:
    """An invitee accepts or declines their own invitation."""
    attendee = (
        MeetingAttendee.objects.select_for_update()
        .select_related("meeting")
        .filter(meeting_id=meeting_id, user=user)
        .first()
    )
    if attendee is None:
        raise NotFoundException(_("You were not invited to this meeting."), code="not_invited")
    if attendee.meeting.status != StaffMeeting.Status.SCHEDULED:
        raise UnprocessableEntity(
            _("This meeting is no longer open for responses."), code="meeting_not_scheduled"
        )
    attendee.response = response
    attendee.responded_at = timezone.now()
    attendee.save(update_fields=["response", "responded_at"])
    return attendee


def next_meeting_for(user, *, now=None) -> StaffMeeting | None:
    """The user's next upcoming scheduled meeting (as an invitee) — surfaced on the
    teacher dashboard."""
    now = now or timezone.now()
    return (
        StaffMeeting.objects.filter(
            attendees__user=user, status=StaffMeeting.Status.SCHEDULED, starts_at__gte=now
        )
        .order_by("starts_at")
        .first()
    )
