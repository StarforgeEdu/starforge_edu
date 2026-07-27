"""ORM-backed schedule repositories."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.schedule import selectors
from apps.schedule.interfaces.repositories import (
    ILessonRepository,
    ILessonTypeRepository,
    IRecurrenceRuleRepository,
    ITermRepository,
    ITimeSlotRepository,
)
from apps.schedule.models import Lesson, LessonType, RecurrenceRule, Term, TimeSlot
from core.repositories import BaseRepository


class TermRepository(BaseRepository[Term], ITermRepository):
    model = Term

    def list_terms(self) -> QuerySet[Term]:
        return Term.objects.all()

    def get(self, *, pk: int) -> Term | None:
        return Term.objects.filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> Term:
        return Term.objects.create(**data)

    def apply_changes(self, term: Term, *, changes: dict[str, Any]) -> Term:
        for field, value in changes.items():
            setattr(term, field, value)
        if changes:
            term.save(update_fields=[*changes.keys(), "updated_at"])
        return term

    def remove(self, term: Term) -> None:
        term.delete()

    def name_taken(self, *, academic_year: str, name: str, exclude_pk: int | None = None) -> bool:
        qs = Term.objects.filter(academic_year=academic_year, name=name)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class TimeSlotRepository(BaseRepository[TimeSlot], ITimeSlotRepository):
    model = TimeSlot

    def list_slots(self) -> QuerySet[TimeSlot]:
        return TimeSlot.objects.select_related("branch")

    def get(self, *, pk: int) -> TimeSlot | None:
        return TimeSlot.objects.select_related("branch").filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> TimeSlot:
        return TimeSlot.objects.create(**data)

    def apply_changes(self, slot: TimeSlot, *, changes: dict[str, Any]) -> TimeSlot:
        for field, value in changes.items():
            setattr(slot, field, value)
        if changes:
            slot.save(update_fields=[*changes.keys()])
        return slot

    def remove(self, slot: TimeSlot) -> None:
        slot.delete()

    def name_taken(self, *, branch_id: int, name: str, exclude_pk: int | None = None) -> bool:
        qs = TimeSlot.objects.filter(branch_id=branch_id, name=name)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class LessonTypeRepository(BaseRepository[LessonType], ILessonTypeRepository):
    model = LessonType

    def list_types(self) -> QuerySet[LessonType]:
        return LessonType.objects.all()

    def get(self, *, pk: int) -> LessonType | None:
        return LessonType.objects.filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> LessonType:
        return LessonType.objects.create(**data)

    def apply_changes(self, lesson_type: LessonType, *, changes: dict[str, Any]) -> LessonType:
        for field, value in changes.items():
            setattr(lesson_type, field, value)
        if changes:
            lesson_type.save(update_fields=[*changes.keys(), "updated_at"])
        return lesson_type

    def remove(self, lesson_type: LessonType) -> None:
        lesson_type.delete()

    def slug_taken(self, *, slug: str, exclude_pk: int | None = None) -> bool:
        qs = LessonType.objects.filter(slug=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class RecurrenceRuleRepository(BaseRepository[RecurrenceRule], IRecurrenceRuleRepository):
    model = RecurrenceRule

    def list_rules(self) -> QuerySet[RecurrenceRule]:
        return RecurrenceRule.objects.select_related("term", "cohort", "teacher__user", "room", "lesson_type")

    def get(self, *, pk: int) -> RecurrenceRule | None:
        return (
            RecurrenceRule.objects.select_related("term", "cohort", "teacher__user", "room", "lesson_type")
            .filter(pk=pk)
            .first()
        )

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[RecurrenceRule]:
        return selectors.scoped_rules(user=user, roles=roles)

    def get_scoped(
        self, *, pk: int, user: Any, roles: set[str] | None
    ) -> RecurrenceRule | None:
        return selectors.scoped_rules(user=user, roles=roles).filter(pk=pk).first()


class LessonRepository(BaseRepository[Lesson], ILessonRepository):
    model = Lesson

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Lesson]:
        return selectors.scoped_lessons(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Lesson | None:
        return selectors.scoped_lessons(user=user, roles=roles).filter(pk=pk).first()
