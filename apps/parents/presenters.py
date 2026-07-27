"""Parent-domain presenters — plain dict mappers (replace the DRF ModelSerializers)."""

from __future__ import annotations

from typing import Any

from apps.parents.models import Guardian, ParentProfile, PickupAuthorization


def parent_to_dict(parent: ParentProfile) -> dict[str, Any]:
    """Personal identity is now OWNED by the parent model (role-native auth), surfaced at
    the top level; ``user`` is retained for the login/username reference + back-compat."""
    return {
        "id": parent.id,
        "username": parent.username,
        "is_active": parent.is_active,
        "must_change_password": parent.must_change_password,
        "last_login_at": parent.last_login_at.isoformat() if parent.last_login_at else None,
        # Identity owned by the parent model.
        "first_name": parent.first_name,
        "last_name": parent.last_name,
        "middle_name": parent.middle_name,
        "full_name": parent.get_full_name(),
        "phone": parent.phone,
        "email": parent.email,
        "birthdate": parent.birthdate.isoformat() if parent.birthdate else None,
        "gender": parent.gender,
        "workplace": parent.workplace,
        "notes": parent.notes,
        "created_at": parent.created_at.isoformat(),
    }


def guardian_to_dict(g: Guardian) -> dict[str, Any]:
    # Denormalized names resolved from the repo's select_related("parent__user",
    # "student__user") — a family link answers "which parent / which child" without
    # a second call, no extra query per row.
    return {
        "id": g.id,
        "parent": g.parent_id,
        "parent_name": g.parent.get_full_name() if g.parent_id else None,
        "student": g.student_id,
        "student_name": g.student.get_full_name() if g.student_id else None,
        "relationship": g.relationship,
        "is_primary": g.is_primary,
        "custody_notes": g.custody_notes,
    }


def pickup_to_dict(p: PickupAuthorization) -> dict[str, Any]:
    # `full_name` is the authorized pickup person; `student_name` (from the repo's
    # select_related("student__user")) names the child being picked up — so the row
    # is self-describing without a second call.
    return {
        "id": p.id,
        "student": p.student_id,
        "student_name": p.student.get_full_name() if p.student_id else None,
        "full_name": p.full_name,
        "phone": p.phone,
        "relationship": p.relationship,
        "is_active": p.is_active,
        "created_at": p.created_at.isoformat(),
    }
