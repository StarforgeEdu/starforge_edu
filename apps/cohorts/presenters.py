"""Cohort presenters — plain dict mappers (replace the DRF ModelSerializers)."""

from __future__ import annotations

from typing import Any

from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
from apps.cohorts.teacher_assignments import (
    MAIN_TEACHER_SLUG,
    legacy_role_for_type,
    resolve_assignment_type,
)
from apps.teachers.models import TeacherType


def teacher_type_to_dict(teacher_type: TeacherType) -> dict[str, Any]:
    return {
        "id": teacher_type.id,
        "name": teacher_type.name,
        "slug": teacher_type.slug,
        "description": teacher_type.description,
        "is_active": teacher_type.is_active,
        "is_system": teacher_type.is_system,
        "is_default": teacher_type.is_default,
        "sort_order": teacher_type.sort_order,
    }


def cohort_teacher_to_dict(ct: CohortTeacher) -> dict[str, Any]:
    teacher_type = resolve_assignment_type(ct)
    return {
        "id": ct.id,
        "teacher": ct.teacher_id,
        "teacher_name": ct.teacher.get_full_name(),
        "teacher_type": teacher_type.id if teacher_type else None,
        "teacher_type_name": teacher_type.name if teacher_type else None,
        "teacher_type_slug": teacher_type.slug if teacher_type else None,
        # Transitional alias for clients that still render the former enum field.
        "role": legacy_role_for_type(teacher_type) if teacher_type else ct.role,
    }


def cohort_to_dict(cohort: Cohort) -> dict[str, Any]:
    # Each bare FK id keeps its readable `_name` companion so a client renders the
    # cohort without a second call. The list queryset select_relateds branch /
    # department / primary_teacher__user / default_room, so these add no query per row.
    assignments = list(cohort.co_teachers.all())
    main_assignments = [
        assignment
        for assignment in assignments
        if (teacher_type := resolve_assignment_type(assignment)) is not None
        and teacher_type.slug == MAIN_TEACHER_SLUG
    ]
    selected_main = next(
        (assignment for assignment in main_assignments if assignment.teacher_id == cohort.primary_teacher_id),
        main_assignments[0] if main_assignments else None,
    )
    canonical_primary = selected_main.teacher if selected_main else None
    primary_teacher = canonical_primary or cohort.primary_teacher
    assignment_payload = [cohort_teacher_to_dict(ct) for ct in assignments]
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
        "primary_teacher": primary_teacher.id if primary_teacher else None,
        "primary_teacher_name": primary_teacher.get_full_name() if primary_teacher else None,
        "default_room": cohort.default_room_id,
        "default_room_name": cohort.default_room.name if cohort.default_room else None,
        "is_archived": cohort.is_archived,
        "teachers": assignment_payload,
        "co_teachers": assignment_payload,
        "created_at": cohort.created_at.isoformat(),
    }


def membership_to_dict(m: CohortMembership) -> dict[str, Any]:
    # `student`/`cohort` are non-null FKs — surface their readable labels alongside the
    # ids. The members-list queryset joins the student profile + cohort (no N+1).
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
