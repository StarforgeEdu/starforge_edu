"""ORM-backed attendance repository (role-scoped reads).

Delegates the nuanced role-based read scoping to ``apps.attendance.selectors`` — the
single read-scoping module (also exercised directly by the tests), so the repository
is a thin, layered adapter over it. It also owns the small lookups the service needs
that touch OTHER apps' models (Lesson, StudentProfile, Cohort) so the service stays
free of direct ORM imports.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.attendance import selectors
from apps.attendance.interfaces.repositories import IAttendanceRepository
from apps.attendance.models import AttendanceRecord
from apps.cohorts.models import Cohort
from apps.schedule.models import Lesson
from apps.students.models import StudentProfile
from core.repositories import BaseRepository


class AttendanceRepository(BaseRepository[AttendanceRecord], IAttendanceRepository):
    model = AttendanceRecord

    def scoped(self, *, user, roles: set[str]) -> QuerySet[AttendanceRecord]:
        return selectors.scoped_records(user=user, roles=roles)

    def get_scoped(self, *, user, roles: set[str], pk: int) -> AttendanceRecord | None:
        return self.scoped(user=user, roles=roles).filter(pk=pk).first()

    def get_lesson(self, *, lesson_id: int) -> Lesson | None:
        return Lesson.objects.filter(pk=lesson_id).first()

    def students_by_ids(self, *, ids: list[int]) -> dict[int, StudentProfile]:
        return {s.pk: s for s in StudentProfile.objects.filter(pk__in=ids)}

    def cohort_taught_by(self, *, cohort_id: int, user) -> bool:
        return (
            Cohort.objects.filter(pk=cohort_id)
            .filter(
                Q(primary_teacher__user=user)
                | Q(co_teachers__teacher__user=user)
                | Q(lessons__teacher__user=user)
            )
            .exists()
        )
