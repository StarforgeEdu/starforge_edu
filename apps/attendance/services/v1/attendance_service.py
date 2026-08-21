"""Attendance service — thin orchestration over the preserved domain functions
(`mark_attendance`) and read selectors (`scoped_records` / `term_summary` /
`cohort_dashboard`), plus FK resolution and the dashboard authz gate."""

from __future__ import annotations

from datetime import datetime

from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _

from apps.attendance import selectors
from apps.attendance.dto.attendance_dto import MarkEntryDTO
from apps.attendance.interfaces.repositories import IAttendanceRepository
from apps.attendance.interfaces.services import IAttendanceService
from apps.attendance.models import AttendanceRecord
from apps.attendance.services import mark_attendance
from apps.schedule.models import Lesson
from core.exceptions import PermissionException, UnprocessableEntity


class AttendanceService(IAttendanceService):
    def __init__(self, repository: IAttendanceRepository) -> None:
        self.repository = repository

    def scoped_records(self, *, user, roles: set[str]) -> QuerySet[AttendanceRecord]:
        return self.repository.scoped(user=user, roles=roles)

    def get_record(self, *, user, roles: set[str], pk: int) -> AttendanceRecord | None:
        return self.repository.get_scoped(user=user, roles=roles, pk=pk)

    def get_lesson(self, *, lesson_id: int, user, roles: set[str]) -> Lesson | None:
        return self.repository.get_lesson(lesson_id=lesson_id, user=user, roles=roles)

    def mark(self, *, lesson: Lesson, entries: list[MarkEntryDTO], actor) -> dict:
        """Resolve each entry's `student_id` to a StudentProfile (unknown id -> 400,
        not a 500), then delegate to the preserved `mark_attendance` domain fn (which
        enforces teacher-scope / correction-window / as-of-date membership)."""
        ids = [e.student_id for e in entries]
        found = self.repository.students_by_ids(ids=ids)
        missing = sorted({sid for sid in ids if sid not in found})
        if missing:
            # Match the domain response for an existing student outside the
            # lesson roster. Distinguishing "unknown" from "not in cohort"
            # turns this write endpoint into a cross-scope student-id oracle.
            raise UnprocessableEntity(
                _("One or more students were not members of this lesson's cohort on the lesson date."),
                code="student_not_in_cohort",
                fields={"students": missing},
            )
        resolved = []
        for entry in entries:
            row = {
                "student": found[entry.student_id],
                "status": entry.status,
                "arrived_at": entry.arrived_at,
                "note": entry.note,
            }
            if entry.card_type is not None:
                row["card_type"] = entry.card_type
            resolved.append(row)
        return mark_attendance(lesson=lesson, entries=resolved, actor=actor)

    def term_summary(self, *, user, roles: set[str], student_id: int, term_id: int) -> dict:
        base = self.repository.scoped(user=user, roles=roles)
        return selectors.term_summary(base_qs=base, student_id=student_id, term_id=term_id)

    def cohort_dashboard(
        self, *, cohort_id: int, date_from: datetime | None, date_to: datetime | None
    ) -> dict:
        return selectors.cohort_dashboard(cohort_id=cohort_id, date_from=date_from, date_to=date_to)

    def authorize_dashboard(self, *, user, roles: set[str], cohort_id: int) -> None:
        """Authorize the cohort-wide feed through the same object scope as records."""
        if selectors.scoped_dashboard_cohorts(user=user, roles=roles).filter(pk=cohort_id).exists():
            return
        raise PermissionException(
            _("You may only view dashboards for cohorts in your scope."), code="out_of_scope"
        )
