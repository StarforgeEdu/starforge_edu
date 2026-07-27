"""Attendance-domain service port."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from django.db.models import QuerySet

from apps.attendance.dto.attendance_dto import MarkEntryDTO
from apps.attendance.models import AttendanceRecord
from apps.schedule.models import Lesson


class IAttendanceService(ABC):
    @abstractmethod
    def scoped_records(self, *, user, roles: set[str]) -> QuerySet[AttendanceRecord]: ...

    @abstractmethod
    def get_record(self, *, user, roles: set[str], pk: int) -> AttendanceRecord | None: ...

    @abstractmethod
    def get_lesson(self, *, lesson_id: int) -> Lesson | None: ...

    @abstractmethod
    def mark(self, *, lesson: Lesson, entries: list[MarkEntryDTO], actor) -> dict: ...

    @abstractmethod
    def term_summary(self, *, user, roles: set[str], student_id: int, term_id: int) -> dict: ...

    @abstractmethod
    def cohort_dashboard(
        self, *, cohort_id: int, date_from: datetime | None, date_to: datetime | None
    ) -> dict: ...

    @abstractmethod
    def authorize_dashboard(self, *, user, roles: set[str], cohort_id: int) -> None: ...
