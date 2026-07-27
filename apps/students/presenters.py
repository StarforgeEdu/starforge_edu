"""Student presenters — plain dict mappers (replace the DRF serializers).

``medical_notes`` is encrypted PHI (TD-11 / DoD #4): it is served ONLY on the
detail/update payloads and ONLY to a DIRECTOR/REGISTRAR (or superuser). The list
payload never carries it. The gate is fail-closed — no request context, no notes.
"""

from __future__ import annotations

from typing import Any

from apps.students.models import EnrollmentEvent, EnrollmentReason, StudentProfile
from core.permissions import Role, get_user_roles

# Roles allowed to read decrypted medical_notes (health data, TD-11 / DoD #4).
MEDICAL_NOTES_ROLES = {Role.DIRECTOR, Role.REGISTRAR}


def can_see_medical_notes(request: Any) -> bool:
    """True only for a superuser or a DIRECTOR/REGISTRAR — fail-closed otherwise."""
    user = getattr(request, "user", None)
    if user is None:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(get_user_roles(request) & MEDICAL_NOTES_ROLES)


def student_to_dict(s: StudentProfile) -> dict[str, Any]:
    """List/action payload (StudentReadSerializer) — deliberately NO medical_notes.

    Personal identity is owned by the student model and surfaced at the top level."""
    return {
        "id": s.id,
        "student_id": s.student_id,
        "username": s.username,
        "is_active": s.is_active,
        "must_change_password": s.must_change_password,
        "last_login_at": s.last_login_at.isoformat() if s.last_login_at else None,
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
        "current_cohort": s.current_cohort_id,
        "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
        "academic_level": s.academic_level,
        "location": s.location,
        "previous_school": s.previous_school,
        "is_blocked": s.is_blocked,
        "blocked_at": s.blocked_at.isoformat() if s.blocked_at else None,
        "block_reason": s.block_reason,
        "emergency_contacts": s.emergency_contacts,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def student_detail_to_dict(s: StudentProfile, *, medical: bool) -> dict[str, Any]:
    """Retrieve/update payload (StudentDetailSerializer): adds medical_notes,
    decrypted only when ``medical`` (the caller passed the role gate)."""
    return {**student_to_dict(s), "medical_notes": s.medical_notes if medical else None}


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
