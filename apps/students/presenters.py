"""Student presenters — plain dict mappers (replace the DRF serializers).

``medical_notes`` and ``emergency_contacts`` are encrypted safeguarding data
(TD-11 / DoD #4). They are served only on detail/update payloads when the exact
membership covering this student grants ``safeguarding:read``. Collection
payloads never carry either field. The gate is fail-closed: no request or
student context means no safeguarding data.
"""

from __future__ import annotations

from typing import Any

from apps.students.models import EnrollmentEvent, EnrollmentReason, StudentProfile
from core.scoping import request_permission_membership_allows


def can_see_safeguarding_data(request: Any, student: StudentProfile | None) -> bool:
    """Whether the caller's safeguarding grant covers this exact student.

    Checking only a role name or the aggregate permission union is unsafe for a
    multi-membership principal: a safeguarding grant in Branch A must not be
    borrowed while a separate students grant makes a Branch B record visible.
    """
    user = getattr(request, "user", None)
    if user is None or student is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    cohort = student.current_cohort if student.current_cohort_id else None
    return request_permission_membership_allows(
        request,
        permission="safeguarding:read",
        branch_id=student.branch_id,
        department_id=cohort.department_id if cohort is not None else None,
        account_kinds={"staff"},
    )


def student_to_dict(s: StudentProfile) -> dict[str, Any]:
    """List/action payload with no medical notes or emergency contacts.

    Personal identity is owned by the student model and surfaced at the top level."""
    current_cohort = s.current_cohort if s.current_cohort_id else None
    return {
        "id": s.id,
        "student_id": s.student_id,
        "username": s.username,
        "is_active": s.is_active,
        # Identity owned by the student model.
        "first_name": s.first_name,
        "last_name": s.last_name,
        "middle_name": s.middle_name,
        "full_name": s.get_full_name(),
        "phone": s.phone,
        "email": s.email,
        "birthdate": s.birthdate.isoformat() if s.birthdate else None,
        "gender": s.gender,
        "status": s.status,
        "branch": s.branch_id,
        "branch_name": s.branch.name if s.branch_id else None,
        "current_cohort": s.current_cohort_id,
        "current_cohort_name": current_cohort.name if current_cohort is not None else None,
        "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
        "academic_level": s.academic_level,
        "location": s.location,
        "previous_school": s.previous_school,
        "is_blocked": s.is_blocked,
        "blocked_at": s.blocked_at.isoformat() if s.blocked_at else None,
        "block_reason": s.block_reason,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def student_list_to_dict(s: StudentProfile) -> dict[str, Any]:
    """Directory payload without detail-only safeguarding and account fields.

    The leadership directory still carries the approved contact/export fields,
    but it must not fan emergency contacts, hold reasons, password-state, or
    sign-in activity across every collection response.
    """
    payload = student_to_dict(s)
    for key in ("block_reason",):
        payload.pop(key, None)
    return payload


def student_detail_to_dict(s: StudentProfile, *, safeguarding: bool) -> dict[str, Any]:
    """Add safeguarding fields only after the exact membership scope gate."""
    return {
        **student_to_dict(s),
        "medical_notes": s.medical_notes if safeguarding else None,
        "emergency_contacts": s.emergency_contacts if safeguarding else None,
    }


# Compatibility name for callers outside the app while the broader safeguarding
# projection replaces the former medical-notes-only contract.
can_see_medical_notes = can_see_safeguarding_data


def enrollment_reason_to_dict(r: EnrollmentReason) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "slug": r.slug,
        "color": r.color,
        "is_active": r.is_active,
    }


def enrollment_event_to_dict(e: EnrollmentEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "from_status": e.from_status,
        "to_status": e.to_status,
        "reason_code": e.reason_code,
        "note": e.note,
        "created_at": e.created_at.isoformat(),
    }
