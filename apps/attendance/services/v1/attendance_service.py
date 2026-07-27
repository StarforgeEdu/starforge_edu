"""Attendance service — thin orchestration over the preserved domain functions
(`mark_attendance`) and read selectors (`scoped_records` / `term_summary` /
`cohort_dashboard`), plus FK resolution and the dashboard authz gate."""

from __future__ import annotations

from datetime import datetime

from django.db.models import QuerySet

from apps.attendance import selectors
from apps.attendance.dto.attendance_dto import MarkEntryDTO
from apps.attendance.interfaces.repositories import IAttendanceRepository
from apps.attendance.interfaces.services import IAttendanceService
from apps.attendance.models import AttendanceRecord
from apps.attendance.services import mark_attendance
from apps.schedule.models import Lesson
from core.exceptions import PermissionException, ValidationException
from core.permissions import Role

# Staff who may view any cohort's dashboard; a teacher additionally qualifies for
# cohorts they teach (checked per-request). Students/parents never do. Kept in sync
# with selectors.STAFF_ROLES.
_DASHBOARD_STAFF = selectors.STAFF_ROLES


class AttendanceService(IAttendanceService):
    def __init__(self, repository: IAttendanceRepository) -> None:
        self.repository = repository

    def scoped_records(self, *, user, roles: set[str]) -> QuerySet[AttendanceRecord]:
        return self.repository.scoped(user=user, roles=roles)

    def get_record(self, *, user, roles: set[str], pk: int) -> AttendanceRecord | None:
        return self.repository.get_scoped(user=user, roles=roles, pk=pk)

    def get_lesson(self, *, lesson_id: int) -> Lesson | None:
        return self.repository.get_lesson(lesson_id=lesson_id)

    def mark(self, *, lesson: Lesson, entries: list[MarkEntryDTO], actor) -> dict:
        """Resolve each entry's `student_id` to a StudentProfile (unknown id -> 400,
        not a 500), then delegate to the preserved `mark_attendance` domain fn (which
        enforces teacher-scope / correction-window / as-of-date membership)."""
        ids = [e.student_id for e in entries]
        found = self.repository.students_by_ids(ids=ids)
        missing = sorted({sid for sid in ids if sid not in found})
        if missing:
            raise ValidationException(
                "One or more student ids do not exist.",
                code="validation_error",
                fields={"student": [f"Unknown student id(s): {missing}."]},
            )
        resolved = [
            {
                "student": found[e.student_id],
                "status": e.status,
                "arrived_at": e.arrived_at,
                "note": e.note,
            }
            for e in entries
        ]
        return mark_attendance(lesson=lesson, entries=resolved, actor=actor)

    def term_summary(self, *, user, roles: set[str], student_id: int, term_id: int) -> dict:
        base = self.repository.scoped(user=user, roles=roles)
        return selectors.term_summary(base_qs=base, student_id=student_id, term_id=term_id)

    def cohort_dashboard(
        self, *, cohort_id: int, date_from: datetime | None, date_to: datetime | None
    ) -> dict:
        return selectors.cohort_dashboard(cohort_id=cohort_id, date_from=date_from, date_to=date_to)

    def authorize_dashboard(self, *, user, roles: set[str], cohort_id: int) -> None:
        """Whole-cohort dashboard is staff-wide (any STAFF_ROLES member) or the teaching
        teacher; a non-teaching, non-staff role (e.g. a student/parent that holds
        attendance:read) is denied with `not_cohort_teacher`."""
        if getattr(user, "is_superuser", False) or roles & _DASHBOARD_STAFF:
            return
        if Role.TEACHER in roles and self.repository.cohort_taught_by(cohort_id=cohort_id, user=user):
            return
        raise PermissionException(
            "You may only view dashboards for cohorts you teach.", code="not_cohort_teacher"
        )
