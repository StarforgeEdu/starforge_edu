"""Schedule-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.schedule.models import Lesson, LessonType, RecurrenceRule, Term, TimeSlot


class ITermService(ABC):
    @abstractmethod
    def list_terms(self) -> QuerySet[Term]: ...

    @abstractmethod
    def get(self, *, pk: int) -> Term | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any]) -> Term: ...

    @abstractmethod
    def update(self, term: Term, *, changes: dict[str, Any]) -> Term: ...

    @abstractmethod
    def delete(self, term: Term) -> None: ...


class ITimeSlotService(ABC):
    @abstractmethod
    def list_slots(self) -> QuerySet[TimeSlot]: ...

    @abstractmethod
    def get(self, *, pk: int) -> TimeSlot | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any]) -> TimeSlot: ...

    @abstractmethod
    def update(self, slot: TimeSlot, *, changes: dict[str, Any]) -> TimeSlot: ...

    @abstractmethod
    def delete(self, slot: TimeSlot) -> None: ...


class ILessonTypeService(ABC):
    @abstractmethod
    def list_types(self) -> QuerySet[LessonType]: ...

    @abstractmethod
    def get(self, *, pk: int) -> LessonType | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any]) -> LessonType: ...

    @abstractmethod
    def update(self, lesson_type: LessonType, *, changes: dict[str, Any]) -> LessonType: ...

    @abstractmethod
    def delete(self, lesson_type: LessonType) -> None: ...


class IRecurrenceRuleService(ABC):
    @abstractmethod
    def list_rules(self) -> QuerySet[RecurrenceRule]: ...

    @abstractmethod
    def get(self, *, pk: int) -> RecurrenceRule | None: ...

    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[RecurrenceRule]: ...

    @abstractmethod
    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> RecurrenceRule | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any], created_by) -> RecurrenceRule: ...

    @abstractmethod
    def update(self, rule: RecurrenceRule, *, changes: dict[str, Any]) -> RecurrenceRule: ...

    @abstractmethod
    def delete(self, rule: RecurrenceRule) -> None: ...

    @abstractmethod
    def bulk_reschedule(self, rule: RecurrenceRule, *, shift_minutes: int, actor) -> int: ...


class ILessonService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Lesson]: ...

    @abstractmethod
    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Lesson | None: ...

    @abstractmethod
    def cancel(self, lesson: Lesson, *, reason: str, actor) -> Lesson: ...

    @abstractmethod
    def move(self, lesson: Lesson, *, starts_at, ends_at, actor) -> Lesson: ...

    @abstractmethod
    def ical_token_for(self, user) -> str: ...

    @abstractmethod
    def lessons_for_token(self, token: str) -> QuerySet[Lesson]: ...

    @abstractmethod
    def build_ical(self, lessons) -> bytes: ...
