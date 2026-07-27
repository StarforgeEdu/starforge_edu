"""Cohort presenters — plain dict mappers (replace the DRF ModelSerializers)."""

from __future__ import annotations

from typing import Any

from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher


def cohort_teacher_to_dict(ct: CohortTeacher) -> dict[str, Any]:
    return {"id": ct.id, "teacher": ct.teacher_id, "role": ct.role}


def cohort_to_dict(cohort: Cohort) -> dict[str, Any]:
    # Each bare FK id keeps its readable `_name` companion so a client renders the
    # cohort without a second call. The list queryset select_relateds branch /
    # department / primary_teacher__user / default_room, so these add no query per row.
    return {
        "id": cohort.id,
        "name": cohort.name,
        "branch": cohort.branch_id,
        "branch_name": cohort.branch.name if cohort.branch_id else None,
        "department": cohort.department_id,
        "department_name": cohort.department.name if cohort.department else None,
        "level": cohort.level,
        "start_date": cohort.start_date.isoformat(),
        "end_date": cohort.end_date.isoformat(),
        "capacity": cohort.capacity,
        "primary_teacher": cohort.primary_teacher_id,
        "primary_teacher_name": (cohort.primary_teacher.get_full_name() if cohort.primary_teacher else None),
        "default_room": cohort.default_room_id,
        "default_room_name": cohort.default_room.name if cohort.default_room else None,
        "is_archived": cohort.is_archived,
        "co_teachers": [cohort_teacher_to_dict(ct) for ct in cohort.co_teachers.all()],
        "created_at": cohort.created_at.isoformat(),
    }


def membership_to_dict(m: CohortMembership) -> dict[str, Any]:
    # `student`/`cohort` are non-null FKs — surface their readable labels alongside the
    # ids. The members-list queryset select_relateds student__user + cohort (no N+1).
    return {
        "id": m.id,
        "cohort": m.cohort_id,
        "cohort_name": m.cohort.name,
        "student": m.student_id,
        "student_name": m.student.get_full_name(),
        "start_date": m.start_date.isoformat(),
        "end_date": m.end_date.isoformat() if m.end_date else None,
        "moved_reason": m.moved_reason,
    }
