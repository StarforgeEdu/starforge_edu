"""Attendance-domain repository ports.

Read scoping is role-based (delegated to ``apps.attendance.selectors``): a
director/HoD/superuser sees every record; a teacher only records on lessons they
teach; a parent their guardian-linked children's; a student their own. Out-of-scope
rows are filtered OUT (so a detail 404s, never a 403 that leaks existence).
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.attendance.models import AttendanceRecord
from apps.schedule.models import Lesson
from apps.students.models import StudentProfile
from core.interfaces import IBaseRepository


class IAttendanceRepository(IBaseRepository[AttendanceRecord]):
    def scoped(self, *, user, roles: set[str]) -> QuerySet[AttendanceRecord]:
        raise NotImplementedError

    def get_scoped(self, *, user, roles: set[str], pk: int) -> AttendanceRecord | None:
        raise NotImplementedError

    def get_lesson(self, *, lesson_id: int) -> Lesson | None:
        raise NotImplementedError

    def students_by_ids(self, *, ids: list[int]) -> dict[int, StudentProfile]:
        raise NotImplementedError

    def cohort_taught_by(self, *, cohort_id: int, user) -> bool:
        raise NotImplementedError
