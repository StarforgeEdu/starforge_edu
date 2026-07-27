"""ORM-backed academics repositories (thin adapters over the preserved selectors)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.academics import selectors
from apps.academics.interfaces.repositories import (
    IExamRepository,
    IExamTypeRepository,
    IGradeRepository,
    ISubjectRepository,
    ITranscriptRepository,
)
from apps.academics.models import Exam, ExamResult, ExamType, Grade, Subject, Transcript
from core.repositories import BaseRepository


class ExamTypeRepository(BaseRepository[ExamType], IExamTypeRepository):
    model = ExamType

    def list_types(self) -> QuerySet[ExamType]:
        return ExamType.objects.all()

    def get(self, *, pk: int) -> ExamType | None:
        return ExamType.objects.filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> ExamType:
        return ExamType.objects.create(**data)

    def apply_changes(self, exam_type: ExamType, *, changes: dict[str, Any]) -> ExamType:
        for field, value in changes.items():
            setattr(exam_type, field, value)
        if changes:
            exam_type.save(update_fields=[*changes.keys(), "updated_at"])
        return exam_type

    def remove(self, exam_type: ExamType) -> None:
        exam_type.delete()

    def slug_taken(self, *, slug: str, exclude_pk: int | None = None) -> bool:
        qs = ExamType.objects.filter(slug=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class SubjectRepository(BaseRepository[Subject], ISubjectRepository):
    model = Subject

    def list_subjects(self) -> QuerySet[Subject]:
        return Subject.objects.select_related("department")

    def get(self, *, pk: int) -> Subject | None:
        return Subject.objects.select_related("department").filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> Subject:
        return Subject.objects.create(**data)

    def apply_changes(self, subject: Subject, *, changes: dict[str, Any]) -> Subject:
        for field, value in changes.items():
            setattr(subject, field, value)
        if changes:
            subject.save(update_fields=[*changes.keys(), "updated_at"])
        return subject

    def remove(self, subject: Subject) -> None:
        subject.delete()

    def code_taken(self, *, code: str, exclude_pk: int | None = None) -> bool:
        qs = Subject.objects.filter(code=code)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class ExamRepository(BaseRepository[Exam], IExamRepository):
    model = Exam

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Exam]:
        return selectors.scoped_exams(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Exam | None:
        return selectors.scoped_exams(user=user, roles=roles).filter(pk=pk).first()

    def add(self, *, data: dict[str, Any]) -> Exam:
        return Exam.objects.create(**data)

    def apply_changes(self, exam: Exam, *, changes: dict[str, Any]) -> Exam:
        for field, value in changes.items():
            setattr(exam, field, value)
        if changes:
            exam.save(update_fields=[*changes.keys(), "updated_at"])
        return exam

    def remove(self, exam: Exam) -> None:
        exam.delete()

    def results_for(self, exam: Exam) -> QuerySet[ExamResult]:
        return exam.results.select_related("student__user")


class GradeRepository(BaseRepository[Grade], IGradeRepository):
    model = Grade

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Grade]:
        return selectors.scoped_grades(user=user, roles=roles)


class TranscriptRepository(BaseRepository[Transcript], ITranscriptRepository):
    model = Transcript

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Transcript]:
        return selectors.scoped_transcripts(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Transcript | None:
        return selectors.scoped_transcripts(user=user, roles=roles).filter(pk=pk).first()
