"""Schedule-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.schedule.models import Lesson, LessonType, RecurrenceRule, Term, TimeSlot
from core.interfaces import IBaseRepository


class ITermRepository(IBaseRepository[Term]):
    def list_terms(self) -> QuerySet[Term]:
        raise NotImplementedError

    def get(self, *, pk: int) -> Term | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> Term:
        raise NotImplementedError

    def apply_changes(self, term: Term, *, changes: dict[str, Any]) -> Term:
        raise NotImplementedError

    def remove(self, term: Term) -> None:
        raise NotImplementedError

    def name_taken(self, *, academic_year: str, name: str, exclude_pk: int | None = None) -> bool:
        raise NotImplementedError


class ITimeSlotRepository(IBaseRepository[TimeSlot]):
    def list_slots(self) -> QuerySet[TimeSlot]:
        raise NotImplementedError

    def get(self, *, pk: int) -> TimeSlot | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> TimeSlot:
        raise NotImplementedError

    def apply_changes(self, slot: TimeSlot, *, changes: dict[str, Any]) -> TimeSlot:
        raise NotImplementedError

    def remove(self, slot: TimeSlot) -> None:
        raise NotImplementedError

    def name_taken(self, *, branch_id: int, name: str, exclude_pk: int | None = None) -> bool:
        raise NotImplementedError


class ILessonTypeRepository(IBaseRepository[LessonType]):
    def list_types(self) -> QuerySet[LessonType]:
        raise NotImplementedError

    def get(self, *, pk: int) -> LessonType | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> LessonType:
        raise NotImplementedError

    def apply_changes(self, lesson_type: LessonType, *, changes: dict[str, Any]) -> LessonType:
        raise NotImplementedError

    def remove(self, lesson_type: LessonType) -> None:
        raise NotImplementedError

    def slug_taken(self, *, slug: str, exclude_pk: int | None = None) -> bool:
        raise NotImplementedError


class IRecurrenceRuleRepository(IBaseRepository[RecurrenceRule]):
    def list_rules(self) -> QuerySet[RecurrenceRule]:
        raise NotImplementedError

    def get(self, *, pk: int) -> RecurrenceRule | None:
        raise NotImplementedError

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[RecurrenceRule]:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> RecurrenceRule | None:
        raise NotImplementedError


class ILessonRepository(IBaseRepository[Lesson]):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Lesson]:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Lesson | None:
        raise NotImplementedError
