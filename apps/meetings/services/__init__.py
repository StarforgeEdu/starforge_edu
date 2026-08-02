"""Staff-meeting services (F3-5): schedule (with invites), cancel, and RSVP."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.meetings.models import MeetingAttendee, StaffMeeting
from core.exceptions import NotFoundException, UnprocessableEntity, ValidationException

MAX_MEETING_ATTENDEES = 200
MAX_MEETING_DURATION = timedelta(hours=24)
MAX_MEETING_AGENDA_CHARS = 20_000


@transaction.atomic
def schedule_meeting(
    *,
    title,
    agenda="",
    starts_at,
    ends_at,
    location="",
    attendees,
    created_by,
    created_by_principal_kind: str | None = None,
    created_by_principal_id: int | None = None,
    branch=None,
) -> StaffMeeting:
    """Create a meeting and invite the given staff (each starts INVITED). Attendees are
    validated as active staff by the serializer."""
    if ends_at <= starts_at:
        raise ValidationException(_("A meeting must end after it starts."), code="meeting_ends_before_start")
    if starts_at < timezone.now() - timedelta(minutes=5):
        raise ValidationException(
            _("A new meeting cannot start in the past."),
            code="validation_error",
            fields={"starts_at": [_("Choose a current or future start time.")]},
        )
    if ends_at - starts_at > MAX_MEETING_DURATION:
        raise ValidationException(
            _("The meeting duration is too long."),
            code="validation_error",
            fields={"ends_at": [_("A meeting may last at most 24 hours.")]},
        )
    if not isinstance(agenda, str) or len(agenda) > MAX_MEETING_AGENDA_CHARS:
        raise ValidationException(
            _("The agenda is too long."),
            code="validation_error",
            fields={"agenda": [_("Must be at most 20000 characters.")]},
        )
    attendees = list(attendees)
    if not attendees or len(attendees) > MAX_MEETING_ATTENDEES:
        raise ValidationException(
            _("Choose between 1 and 200 attendees."),
            code="validation_error",
            fields={"attendees": [_("Choose between 1 and 200 attendees.")]},
        )
    if created_by is None and (created_by_principal_kind is not None or created_by_principal_id is not None):
        raise ValidationException(
            _("Invalid meeting creator attribution."),
            code="validation_error",
            fields={"created_by": [_("Clear the creator and its role attribution together.")]},
        )
    if created_by is not None and (created_by_principal_kind is None or created_by_principal_id is None):
        from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

        creator_principal = resolve_unambiguous_user_principal(
            created_by.id,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="created_by",
            message=_("The meeting creator does not identify one active staff role account."),
        )
        created_by_principal_kind = creator_principal.kind
        created_by_principal_id = creator_principal.principal_id
    elif created_by is not None:
        from core.role_principals import STAFF_PRINCIPAL_KINDS, validate_role_principal

        validate_role_principal(
            kind=str(created_by_principal_kind),
            principal_id=cast(int, created_by_principal_id),
            user_id=created_by.pk,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="created_by",
        )
    meeting = StaffMeeting.objects.create(
        title=title,
        agenda=agenda,
        branch=branch,
        starts_at=starts_at,
        ends_at=ends_at,
        location=location,
        created_by=created_by,
        created_by_principal_kind=created_by_principal_kind or "",
        created_by_principal_id=created_by_principal_id,
        created_by_attribution_status=(
            StaffMeeting.ActorAttributionStatus.CAPTURED
            if created_by_principal_id is not None
            else StaffMeeting.ActorAttributionStatus.QUARANTINED
        ),
    )
    from apps.meetings.dto.meeting_dto import MeetingInvitee
    from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

    # Dedupe the role-principal list so a repeated selector cannot trip the unique
    # constraints. Legacy internal callers may still pass User rows; resolve those
    # only when the staff/teacher principal is unambiguous.
    seen: set[int] = set()
    rows = []
    for candidate in attendees:
        if isinstance(candidate, MeetingInvitee):
            invitee = candidate
        else:
            principal = resolve_unambiguous_user_principal(
                candidate.id,
                allowed_kinds=STAFF_PRINCIPAL_KINDS,
                field="attendees",
                message=_("An attendee does not identify one active staff role account."),
            )
            invitee = MeetingInvitee(
                user=candidate,
                principal_kind=principal.kind,
                principal_id=principal.principal_id,
            )
        if invitee.user.id in seen:
            continue
        seen.add(invitee.user.id)
        rows.append(
            MeetingAttendee(
                meeting=meeting,
                user=invitee.user,
                principal_kind=invitee.principal_kind,
                principal_id=invitee.principal_id,
            )
        )
    MeetingAttendee.objects.bulk_create(rows)
    return meeting


@transaction.atomic
def cancel_meeting(
    *,
    meeting_id: int,
    actor,
    actor_principal_kind: str | None = None,
    actor_principal_id: int | None = None,
) -> StaffMeeting:
    meeting = StaffMeeting.objects.select_for_update().filter(pk=meeting_id).first()
    if meeting is None:
        raise NotFoundException(_("Meeting not found."), code="meeting_not_found")
    if meeting.status == StaffMeeting.Status.CANCELLED:
        return meeting
    if meeting.status != StaffMeeting.Status.SCHEDULED:
        raise UnprocessableEntity(
            _("Only a scheduled meeting can be cancelled."), code="meeting_not_scheduled"
        )
    if actor_principal_kind is None or actor_principal_id is None:
        from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

        actor_principal = resolve_unambiguous_user_principal(
            actor.id,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="actor",
            message=_("The meeting actor does not identify one active staff role account."),
        )
        actor_principal_kind = actor_principal.kind
        actor_principal_id = actor_principal.principal_id
    else:
        from core.role_principals import STAFF_PRINCIPAL_KINDS, validate_role_principal

        validate_role_principal(
            kind=actor_principal_kind,
            principal_id=actor_principal_id,
            user_id=actor.pk,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="actor",
        )
    meeting.status = StaffMeeting.Status.CANCELLED
    meeting.cancelled_by = actor
    meeting.cancelled_by_principal_kind = actor_principal_kind
    meeting.cancelled_by_principal_id = actor_principal_id
    meeting.cancelled_by_attribution_status = StaffMeeting.ActorAttributionStatus.CAPTURED
    meeting.cancelled_at = timezone.now()
    meeting.save(
        update_fields=[
            "status",
            "cancelled_by",
            "cancelled_by_principal_kind",
            "cancelled_by_principal_id",
            "cancelled_by_attribution_status",
            "cancelled_at",
        ]
    )
    return meeting


@transaction.atomic
def respond_to_meeting(
    *,
    meeting_id: int,
    user,
    response: str,
    principal_kind: str | None = None,
    principal_id: int | None = None,
) -> MeetingAttendee:
    """An invitee accepts or declines their own invitation."""
    meeting = StaffMeeting.objects.select_for_update().filter(pk=meeting_id).first()
    if meeting is None:
        raise NotFoundException(_("Meeting not found."), code="meeting_not_found")
    if response not in (MeetingAttendee.Response.ACCEPTED, MeetingAttendee.Response.DECLINED):
        raise ValidationException(
            _("Invalid meeting response."),
            code="validation_error",
            fields={"response": [_("Choose accepted or declined.")]},
        )
    if principal_kind is None or principal_id is None:
        from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

        principal = resolve_unambiguous_user_principal(
            user.id,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="attendee",
            message=_("The attendee does not identify one active staff role account."),
        )
        principal_kind, principal_id = principal.kind, principal.principal_id
    attendee = (
        MeetingAttendee.objects.select_for_update()
        .filter(
            meeting_id=meeting_id,
            user=user,
            principal_kind=principal_kind,
            principal_id=principal_id,
        )
        .first()
    )
    if attendee is None:
        raise NotFoundException(_("You were not invited to this meeting."), code="not_invited")
    if meeting.status != StaffMeeting.Status.SCHEDULED:
        raise UnprocessableEntity(
            _("This meeting is no longer open for responses."), code="meeting_not_scheduled"
        )
    if attendee.response == response:
        return attendee
    attendee.response = response
    attendee.responded_at = timezone.now()
    attendee.save(update_fields=["response", "responded_at"])
    return attendee


def next_meeting_for(
    user,
    *,
    principal_kind: str | None = None,
    principal_id: int | None = None,
    now=None,
) -> StaffMeeting | None:
    """The user's next upcoming scheduled meeting (as an invitee) — surfaced on the
    teacher dashboard."""
    if principal_kind is None or principal_id is None:
        from core.role_principals import STAFF_PRINCIPAL_KINDS, resolve_unambiguous_user_principal

        principal = resolve_unambiguous_user_principal(
            user.id,
            allowed_kinds=STAFF_PRINCIPAL_KINDS,
            field="attendee",
            message=_("The attendee does not identify one active staff role account."),
        )
        principal_kind, principal_id = principal.kind, principal.principal_id
    now = now or timezone.now()
    return (
        StaffMeeting.objects.filter(
            attendees__principal_kind=principal_kind,
            attendees__principal_id=principal_id,
            status=StaffMeeting.Status.SCHEDULED,
            starts_at__gte=now,
        )
        .order_by("starts_at")
        .first()
    )
