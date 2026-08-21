"""Meeting-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from apps.meetings.models import MeetingAttendee, StaffMeeting


def _principal_identity(
    user,
    *,
    kind: str,
    principal_id: int | None,
    attribution_status: str,
) -> dict[str, Any] | None:
    if (
        attribution_status not in {"captured", "resolved"}
        or principal_id is None
        or kind not in {"staff", "teacher"}
    ):
        return None
    display_name = None
    if user is not None:
        relation = "staff_profile" if kind == "staff" else "teacher_profile"
        try:
            profile = getattr(user, relation)
        except ObjectDoesNotExist:
            profile = None
        if profile is not None and profile.pk == principal_id:
            display_name = (
                " ".join(
                    value.strip()
                    for value in (profile.first_name, profile.middle_name, profile.last_name)
                    if isinstance(value, str) and value.strip()
                )
                or profile.username
            )
    return {
        "kind": kind,
        "id": principal_id,
        "display_name": display_name,
        "account_label": "Teacher" if kind == "teacher" else "Staff",
    }


def attendee_to_dict(a: MeetingAttendee, *, include_identity: bool) -> dict[str, Any]:
    data = {
        "id": a.id,
        "response": a.response,
        "responded_at": a.responded_at.isoformat() if a.responded_at else None,
    }
    if include_identity:
        data["principal"] = _principal_identity(
            a.user,
            kind=a.principal_kind,
            principal_id=a.principal_id,
            attribution_status="captured",
        )
    return data


def meeting_to_dict(
    m: StaffMeeting,
    *,
    include_all_attendees: bool = True,
    principal_kind: str = "",
    principal_id: int | None = None,
) -> dict[str, Any]:
    all_attendees = list(m.attendees.all())
    attributed_attendees = [
        attendee
        for attendee in all_attendees
        if attendee.principal_kind in {"staff", "teacher"} and attendee.principal_id is not None
    ]
    attendees = attributed_attendees
    if not include_all_attendees:
        attendees = [
            attendee
            for attendee in attendees
            if attendee.principal_kind == principal_kind and attendee.principal_id == principal_id
        ]
    data = {
        "id": m.id,
        "title": m.title,
        "agenda": m.agenda,
        "branch": m.branch_id,
        "branch_name": m.branch.name if m.branch is not None else None,
        "starts_at": m.starts_at.isoformat(),
        "ends_at": m.ends_at.isoformat(),
        "location": m.location,
        "status": m.status,
        "attendee_count": len(all_attendees),
        "attendees": [attendee_to_dict(a, include_identity=include_all_attendees) for a in attendees],
        "cancelled_at": m.cancelled_at.isoformat() if m.cancelled_at else None,
        "created_at": m.created_at.isoformat(),
    }
    if include_all_attendees:
        creator = _principal_identity(
            m.created_by,
            kind=m.created_by_principal_kind,
            principal_id=m.created_by_principal_id,
            attribution_status=m.created_by_attribution_status,
        )
        canceller = _principal_identity(
            m.cancelled_by,
            kind=m.cancelled_by_principal_kind,
            principal_id=m.cancelled_by_principal_id,
            attribution_status=m.cancelled_by_attribution_status,
        )
        data.update(
            {
                "created_by": creator,
                "created_by_attribution_status": m.created_by_attribution_status,
                "cancelled_by": canceller,
                "cancelled_by_attribution_status": m.cancelled_by_attribution_status,
                "unresolved_attendee_count": len(all_attendees) - len(attributed_attendees),
            }
        )
    return data
