"""Meeting-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from apps.meetings.models import MeetingAttendee, StaffMeeting


def attendee_to_dict(a: MeetingAttendee) -> dict[str, Any]:
    return {
        "id": a.id,
        "user": a.user_id,
        "response": a.response,
        "responded_at": a.responded_at.isoformat() if a.responded_at else None,
    }


def meeting_to_dict(m: StaffMeeting) -> dict[str, Any]:
    return {
        "id": m.id,
        "title": m.title,
        "agenda": m.agenda,
        "branch": m.branch_id,
        "starts_at": m.starts_at.isoformat(),
        "ends_at": m.ends_at.isoformat(),
        "location": m.location,
        "status": m.status,
        "attendees": [attendee_to_dict(a) for a in m.attendees.all()],
        "created_by": m.created_by_id,
        "cancelled_by": m.cancelled_by_id,
        "cancelled_at": m.cancelled_at.isoformat() if m.cancelled_at else None,
        "created_at": m.created_at.isoformat(),
    }
