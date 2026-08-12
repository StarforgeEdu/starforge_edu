"""ORM-backed attendance repository (role-scoped reads).

Delegates the nuanced role-based read scoping to ``apps.attendance.selectors`` — the
single read-scoping module (also exercised directly by the tests), so the repository
is a thin, layered adapter over it. It also owns the small lookups the service needs
that touch OTHER apps' models (Lesson, StudentProfile, Cohort) so the service stays
free of direct ORM imports.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.attendance import selectors
from apps.attendance.interfaces.repositories import IAttendanceRepository
from apps.attendance.models import AttendanceRecord
from apps.cohorts.selectors import taught_cohorts
from apps.schedule import selectors as schedule_selectors
from apps.schedule.models import Lesson
from apps.students.models import StudentProfile
from core.repositories import BaseRepository


class AttendanceRepository(BaseRepository[AttendanceRecord], IAttendanceRepository):
    model = AttendanceRecord

    def scoped(self, *, user, roles: set[str]) -> QuerySet[AttendanceRecord]:
        return selectors.scoped_records(user=user, roles=roles)

    def get_scoped(self, *, user, roles: set[str], pk: int) -> AttendanceRecord | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()

    def get_lesson(self, *, lesson_id: int, user, roles: set[str]) -> Lesson | None:
        # Resolve the action target through the exact attendance-write scope,
        # not through schedule:read.  Otherwise a read grant in Branch B could
        # expose a lesson while attendance:write was held only in Branch A.
        return (
            schedule_selectors.scoped_lessons(
                user=user,
                roles=roles,
                permission="attendance:write",
            )
            .filter(pk=lesson_id)
            .first()
        )

    def students_by_ids(self, *, ids: list[int]) -> dict[int, StudentProfile]:
        return {s.pk: s for s in StudentProfile.objects.filter(pk__in=ids)}

    def cohort_taught_by(self, *, cohort_id: int, user) -> bool:
        return taught_cohorts(
            user=user,
            include_lesson_teacher=False,
        ).filter(pk=cohort_id).exists()
