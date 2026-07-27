"""Org-domain presenters — plain dict mappers (replace the DRF ModelSerializers).

Decimals are rendered as strings and times as ``HH:MM:SS`` to match the previous
DRF output exactly (DRF's default COERCE_DECIMAL_TO_STRING + TimeField format).
"""

from __future__ import annotations

from typing import Any

from django.apps import apps as django_apps

from apps.org.models import (
    Branch,
    BranchHoliday,
    BranchTransfer,
    BranchWorkingHours,
    CenterSettings,
    Department,
    Room,
)


def _dec(value) -> str | None:
    return str(value) if value is not None else None


def department_to_dict(d: Department) -> dict[str, Any]:
    teacher = getattr(d.head, "teacher_profile", None) if d.head else None
    return {
        "id": d.id,
        "branch": d.branch_id,
        # Readable companions so a client need not fetch the branch/head separately.
        # branch/head are select_related on the department list + prefetched
        # (departments__head) when nested under a branch, so no extra query per row.
        "branch_name": d.branch.name if d.branch_id else None,
        "name": d.name,
        "slug": d.slug,
        "description": d.description,
        "is_active": d.is_active,
        "head": teacher.pk if teacher is not None else None,
        # Object-guard (not head_id): a null FK short-circuits with no query, a set
        # FK is select_related/prefetched — and it narrows the Optional for the checker.
        "head_name": teacher.get_full_name() if teacher is not None else None,
        "budget": _dec(d.budget),
        "created_at": d.created_at.isoformat(),
    }


def working_hour_to_dict(w: BranchWorkingHours) -> dict[str, Any]:
    return {
        "id": w.id,
        "weekday": w.weekday,
        "opens_at": w.opens_at.isoformat(),
        "closes_at": w.closes_at.isoformat(),
        "is_closed": w.is_closed,
    }


def holiday_to_dict(h: BranchHoliday) -> dict[str, Any]:
    return {
        "id": h.id,
        "date": h.date.isoformat(),
        "name": h.name,
        "is_working_day_override": h.is_working_day_override,
    }


def room_to_dict(r: Room) -> dict[str, Any]:
    return {
        "id": r.id,
        "branch": r.branch_id,
        # Readable companion; the room list select_related("branch"), so no extra query.
        "branch_name": r.branch.name if r.branch_id else None,
        "name": r.name,
        "capacity": r.capacity,
        "equipment": r.equipment,
        "is_active": r.is_active,
        "notes": r.notes,
        "created_at": r.created_at.isoformat(),
    }


def branch_to_dict(b: Branch) -> dict[str, Any]:
    return {
        "id": b.id,
        "name": b.name,
        "slug": b.slug,
        "address": b.address,
        "phone": b.phone,
        "timezone": b.timezone,
        "is_active": b.is_active,
        "max_students": b.max_students,
        "max_teachers": b.max_teachers,
        "archived_at": b.archived_at.isoformat() if b.archived_at else None,
        "departments": [department_to_dict(d) for d in b.departments.all()],
        "working_hours": [working_hour_to_dict(w) for w in b.working_hours.all()],
        "created_at": b.created_at.isoformat(),
    }


def branch_capacity_status(b: Branch) -> dict[str, Any]:
    try:
        StudentProfile = django_apps.get_model("students", "StudentProfile")
    except LookupError:
        current = 0
    else:
        current = (
            StudentProfile.objects.filter(branch=b).exclude(status__in=("graduated", "withdrawn")).count()
        )
    return {
        "current_students": current,
        "max_students": b.max_students,
        "over": b.max_students is not None and current > b.max_students,
    }


def branch_detail_to_dict(b: Branch) -> dict[str, Any]:
    return {**branch_to_dict(b), "capacity_status": branch_capacity_status(b)}


def transfer_to_dict(t: BranchTransfer) -> dict[str, Any]:
    # Readable companions for every FK — the transfer list select_related("from_branch",
    # "to_branch", "user", "actor"), so these add JOINs, not queries. actor is nullable.
    return {
        "id": t.id,
        "user": t.user_id,
        "user_name": t.user.get_full_name() if t.user_id else None,
        "from_branch": t.from_branch_id,
        "from_branch_name": t.from_branch.name if t.from_branch_id else None,
        "to_branch": t.to_branch_id,
        "to_branch_name": t.to_branch.name if t.to_branch_id else None,
        "reason": t.reason,
        "actor": t.actor_id,
        "actor_name": t.actor.get_full_name() if t.actor else None,
        "created_at": t.created_at.isoformat(),
    }


# The writable + read (updated_at) fields the settings endpoint exposes (TD-13 —
# mirrors CenterSettingsSerializer.Meta.fields, never __all__).
_SETTINGS_INT_FIELDS = (
    "late_threshold_minutes",
    "attendance_correction_window_hours",
    "auto_absent_after_minutes",
    "assignment_grace_minutes",
    "assignment_max_resubmits",
    "max_upload_mb",
    "storage_quota_gb",
    "payment_reminder_interval_days",
    "otp_cooldown_seconds",
    "penalty_escalation_threshold",
)
_SETTINGS_BOOL_FIELDS = (
    "open_registration",
    "require_group_acceptance",
    "ai_exam_generation_enabled",
    "show_classroom_rank",
    "placement_test_creation_mobile_only",
)
_SETTINGS_STR_FIELDS = (
    "grading_scheme",
    "currency_primary",
    "currency_secondary",
    "fx_source",
    "student_id_pattern",
    "center_code",
)
_SETTINGS_DEC_FIELDS = (
    "honor_roll_min",
    "academic_warning_max",
    "fx_rate_usd_manual",
    "sibling_discount_percent",
)
_SETTINGS_JSON_FIELDS = (
    "allowed_file_types",
    "otp_channel_prefs",
    "placement_allowed_question_types",
)


def settings_to_dict(s: CenterSettings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in _SETTINGS_INT_FIELDS + _SETTINGS_BOOL_FIELDS + _SETTINGS_STR_FIELDS + _SETTINGS_JSON_FIELDS:
        out[f] = getattr(s, f)
    for f in _SETTINGS_DEC_FIELDS:
        out[f] = _dec(getattr(s, f))
    out["quiet_hours_start"] = s.quiet_hours_start.isoformat()
    out["quiet_hours_end"] = s.quiet_hours_end.isoformat()
    out["updated_at"] = s.updated_at.isoformat()
    return out


def staff_to_dict(staff) -> dict[str, Any]:
    """Role-native staff payload; the internal User bridge is intentionally absent."""
    memberships = [rm for rm in staff.user.role_memberships.all() if rm.revoked_at is None]
    return {
        "id": staff.id,
        "username": staff.username,
        "first_name": staff.first_name,
        "last_name": staff.last_name,
        "middle_name": staff.middle_name,
        "full_name": staff.get_full_name(),
        "phone": staff.phone,
        "email": staff.email,
        "birthdate": staff.birthdate.isoformat() if staff.birthdate else None,
        "gender": staff.gender,
        "is_active": staff.is_active,
        "must_change_password": staff.must_change_password,
        "last_login_at": staff.last_login_at.isoformat() if staff.last_login_at else None,
        "role_memberships": [
            {
                "id": membership.id,
                "role": membership.role,
                "branch": membership.branch_id,
                "department": membership.department_id,
                "granted_at": membership.granted_at.isoformat(),
            }
            for membership in memberships
        ],
        "created_at": staff.created_at.isoformat(),
        "updated_at": staff.updated_at.isoformat(),
    }
