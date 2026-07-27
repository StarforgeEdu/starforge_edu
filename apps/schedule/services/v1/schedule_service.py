"""Schedule application services (staff CRUD + delegation to the preserved
recurrence/occurrence/iCal domain functions in apps.schedule.services)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet
from django.utils.text import slugify

from apps.cohorts.models import Cohort
from apps.org.models import Branch, Room
from apps.schedule import services as domain
from apps.schedule.interfaces.repositories import (
    ILessonRepository,
    ILessonTypeRepository,
    IRecurrenceRuleRepository,
    ITermRepository,
    ITimeSlotRepository,
)
from apps.schedule.interfaces.services import (
    ILessonService,
    ILessonTypeService,
    IRecurrenceRuleService,
    ITermService,
    ITimeSlotService,
)
from apps.schedule.models import Lesson, LessonType, RecurrenceRule, Term, TimeSlot
from apps.teachers.models import TeacherProfile
from core.exceptions import ValidationException


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


class TermService(ITermService):
    def __init__(self, repository: ITermRepository) -> None:
        self.repository = repository

    def list_terms(self) -> QuerySet[Term]:
        return self.repository.list_terms()

    def get(self, *, pk: int) -> Term | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> Term:
        if self.repository.name_taken(academic_year=data["academic_year"], name=data["name"]):
            raise _reject("name", "A term with this name already exists for this academic year.")
        return self.repository.add(data=data)

    def update(self, term: Term, *, changes: dict[str, Any]) -> Term:
        academic_year = changes.get("academic_year", term.academic_year)
        name = changes.get("name", term.name)
        if ("academic_year" in changes or "name" in changes) and self.repository.name_taken(
            academic_year=academic_year, name=name, exclude_pk=term.pk
        ):
            raise _reject("name", "A term with this name already exists for this academic year.")
        return self.repository.apply_changes(term, changes=changes)

    def delete(self, term: Term) -> None:
        self.repository.remove(term)


class TimeSlotService(ITimeSlotService):
    def __init__(self, repository: ITimeSlotRepository) -> None:
        self.repository = repository

    def list_slots(self) -> QuerySet[TimeSlot]:
        return self.repository.list_slots()

    def get(self, *, pk: int) -> TimeSlot | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> TimeSlot:
        branch_id = data["branch_id"]
        if not Branch.objects.filter(pk=branch_id).exists():
            raise _reject("branch", "Branch does not exist.")
        if self.repository.name_taken(branch_id=branch_id, name=data["name"]):
            raise _reject("name", "A time slot with this name already exists in this branch.")
        return self.repository.add(data=data)

    def update(self, slot: TimeSlot, *, changes: dict[str, Any]) -> TimeSlot:
        branch_id = changes.get("branch_id", slot.branch_id)
        name = changes.get("name", slot.name)
        if "branch_id" in changes and not Branch.objects.filter(pk=branch_id).exists():
            raise _reject("branch", "Branch does not exist.")
        if ("branch_id" in changes or "name" in changes) and self.repository.name_taken(
            branch_id=branch_id, name=name, exclude_pk=slot.pk
        ):
            raise _reject("name", "A time slot with this name already exists in this branch.")
        return self.repository.apply_changes(slot, changes=changes)

    def delete(self, slot: TimeSlot) -> None:
        self.repository.remove(slot)


class LessonTypeService(ILessonTypeService):
    def __init__(self, repository: ILessonTypeRepository) -> None:
        self.repository = repository

    def list_types(self) -> QuerySet[LessonType]:
        return self.repository.list_types()

    def get(self, *, pk: int) -> LessonType | None:
        return self.repository.get(pk=pk)

    def create(self, *, data: dict[str, Any]) -> LessonType:
        data = dict(data)
        if not data.get("slug"):
            # Auto-derive from the label so managers just type a name (F3-1 parity).
            data["slug"] = slugify(data.get("name", ""))[:64]
        if not data["slug"]:
            raise _reject("slug", "Could not derive a slug; provide one explicitly.")
        if self.repository.slug_taken(slug=data["slug"]):
            raise _reject("slug", "A lesson type with this slug already exists.")
        return self.repository.add(data=data)

    def update(self, lesson_type: LessonType, *, changes: dict[str, Any]) -> LessonType:
        if "slug" in changes and self.repository.slug_taken(slug=changes["slug"], exclude_pk=lesson_type.pk):
            raise _reject("slug", "A lesson type with this slug already exists.")
        return self.repository.apply_changes(lesson_type, changes=changes)

    def delete(self, lesson_type: LessonType) -> None:
        self.repository.remove(lesson_type)


# FK spec for recurrence-rule writes: (field name, model, whether required on create).
_RULE_FKS: tuple[tuple[str, Any], ...] = (
    ("term", Term),
    ("cohort", Cohort),
    ("teacher", TeacherProfile),
    ("room", Room),
    ("lesson_type", LessonType),
)


class RecurrenceRuleService(IRecurrenceRuleService):
    def __init__(self, repository: IRecurrenceRuleRepository) -> None:
        self.repository = repository

    def list_rules(self) -> QuerySet[RecurrenceRule]:
        return self.repository.list_rules()

    def get(self, *, pk: int) -> RecurrenceRule | None:
        return self.repository.get(pk=pk)

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[RecurrenceRule]:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(
        self, *, pk: int, user: Any, roles: set[str] | None
    ) -> RecurrenceRule | None:
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve_fks(self, data: dict[str, Any]) -> dict[str, Any]:
        """Replace any present FK-id value with the ORM object, raising a clean 400
        (never a 500 IntegrityError) when an id references a missing row."""
        out = dict(data)
        for field, model in _RULE_FKS:
            if field not in out:
                continue
            value = out[field]
            if value is None:
                continue  # nullable room/lesson_type
            obj = model.objects.filter(pk=value).first()
            if obj is None:
                raise _reject(field, f"{field} does not exist.")
            out[field] = obj
        return out

    def create(self, *, data: dict[str, Any], created_by) -> RecurrenceRule:
        return domain.create_rule(created_by=created_by, **self._resolve_fks(data))

    def update(self, rule: RecurrenceRule, *, changes: dict[str, Any]) -> RecurrenceRule:
        return domain.update_rule(rule, **self._resolve_fks(changes))

    def delete(self, rule: RecurrenceRule) -> None:
        rule.delete()

    def bulk_reschedule(self, rule: RecurrenceRule, *, shift_minutes: int, actor) -> int:
        return domain.bulk_reschedule(rule, shift_minutes=shift_minutes, actor=actor)


class LessonService(ILessonService):
    def __init__(self, repository: ILessonRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Lesson]:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Lesson | None:
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def cancel(self, lesson: Lesson, *, reason: str, actor) -> Lesson:
        return domain.cancel_occurrence(lesson, reason=reason, actor=actor)

    def move(self, lesson: Lesson, *, starts_at, ends_at, actor) -> Lesson:
        return domain.move_occurrence(lesson, starts_at=starts_at, ends_at=ends_at, actor=actor)

    def ical_token_for(self, user) -> str:
        return domain.ical_token_for(user)

    def lessons_for_token(self, token: str) -> QuerySet[Lesson]:
        return domain.lessons_for_token(token)

    def build_ical(self, lessons) -> bytes:
        return domain.build_ical(lessons)
