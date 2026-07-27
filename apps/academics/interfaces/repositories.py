"""Academics-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.academics.models import Exam, ExamResult, ExamType, Grade, Subject, Transcript
from core.interfaces import IBaseRepository


class IExamTypeRepository(IBaseRepository[ExamType]):
    def list_types(self) -> QuerySet[ExamType]:
        raise NotImplementedError

    def get(self, *, pk: int) -> ExamType | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> ExamType:
        raise NotImplementedError

    def apply_changes(self, exam_type: ExamType, *, changes: dict[str, Any]) -> ExamType:
        raise NotImplementedError

    def remove(self, exam_type: ExamType) -> None:
        raise NotImplementedError

    def slug_taken(self, *, slug: str, exclude_pk: int | None = None) -> bool:
        raise NotImplementedError


class ISubjectRepository(IBaseRepository[Subject]):
    def list_subjects(self) -> QuerySet[Subject]:
        raise NotImplementedError

    def get(self, *, pk: int) -> Subject | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> Subject:
        raise NotImplementedError

    def apply_changes(self, subject: Subject, *, changes: dict[str, Any]) -> Subject:
        raise NotImplementedError

    def remove(self, subject: Subject) -> None:
        raise NotImplementedError

    def code_taken(self, *, code: str, exclude_pk: int | None = None) -> bool:
        raise NotImplementedError


class IExamRepository(IBaseRepository[Exam]):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Exam]:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Exam | None:
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]) -> Exam:
        raise NotImplementedError

    def apply_changes(self, exam: Exam, *, changes: dict[str, Any]) -> Exam:
        raise NotImplementedError

    def remove(self, exam: Exam) -> None:
        raise NotImplementedError

    def results_for(self, exam: Exam) -> QuerySet[ExamResult]:
        raise NotImplementedError


class IGradeRepository(IBaseRepository[Grade]):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Grade]:
        raise NotImplementedError


class ITranscriptRepository(IBaseRepository[Transcript]):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Transcript]:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> Transcript | None:
        raise NotImplementedError
