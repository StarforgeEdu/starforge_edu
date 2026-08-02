"""Academics-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.academics.integrity import ExamReadiness
from apps.academics.models import (
    Exam,
    ExamLifecycleEvent,
    ExamResult,
    ExamType,
    Grade,
    Subject,
    Transcript,
)


class IExamTypeService(ABC):
    @abstractmethod
    def list_types(self) -> QuerySet[ExamType]: ...

    @abstractmethod
    def get(self, *, pk: int) -> ExamType | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any], actor=None) -> ExamType: ...

    @abstractmethod
    def update(self, exam_type: ExamType, *, changes: dict[str, Any], actor=None) -> ExamType: ...

    @abstractmethod
    def delete(self, exam_type: ExamType, *, actor=None) -> None: ...


class ISubjectService(ABC):
    @abstractmethod
    def list_subjects(self) -> QuerySet[Subject]: ...

    @abstractmethod
    def get(self, *, pk: int) -> Subject | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any], actor=None) -> Subject: ...

    @abstractmethod
    def update(self, subject: Subject, *, changes: dict[str, Any], actor=None) -> Subject: ...

    @abstractmethod
    def delete(self, subject: Subject, *, actor=None) -> None: ...


class IExamService(ABC):
    @abstractmethod
    def scoped(
        self,
        *,
        user: Any,
        roles: set[str] | None,
        permission: str = "academics:read",
    ) -> QuerySet[Exam]: ...

    @abstractmethod
    def get_scoped(
        self,
        *,
        pk: int,
        user: Any,
        roles: set[str] | None,
        permission: str = "academics:read",
    ) -> Exam | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any], writable_cohort_ids, created_by=None) -> Exam: ...

    @abstractmethod
    def update(
        self,
        exam: Exam,
        *,
        changes: dict[str, Any],
        writable_cohort_ids,
        actor=None,
    ) -> Exam: ...

    @abstractmethod
    def delete(self, exam: Exam, *, actor=None) -> None: ...

    @abstractmethod
    def results_for(self, exam: Exam) -> QuerySet[ExamResult]: ...

    @abstractmethod
    def record_results(self, *, exam: Exam, rows: list[dict], actor) -> dict: ...

    @abstractmethod
    def import_csv(self, *, exam: Exam, csv_file, actor) -> dict: ...

    @abstractmethod
    def publish(
        self,
        *,
        exam: Exam,
        actor,
        expected_version: int,
        confirmed: bool,
    ) -> tuple[Exam, ExamReadiness]: ...

    @abstractmethod
    def readiness(self, *, exam: Exam) -> ExamReadiness: ...

    @abstractmethod
    def correct(
        self,
        *,
        exam: Exam,
        changes: dict[str, Any],
        rows: list[dict[str, Any]],
        reason: str,
        expected_version: int,
        writable_cohort_ids,
        actor,
    ) -> tuple[Exam, ExamLifecycleEvent]: ...

    @abstractmethod
    def history(self, *, exam: Exam) -> QuerySet[ExamLifecycleEvent]: ...


class IGradeService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Grade]: ...

    @abstractmethod
    def recompute(self, *, cohort, subject, term, publish: bool) -> list[Grade]: ...

    @abstractmethod
    def honor_roll(self, *, term_id: int, user, roles: set[str] | None) -> QuerySet[Grade]: ...

    @abstractmethod
    def warnings(self, *, term_id: int, user, roles: set[str] | None) -> QuerySet[Grade]: ...


class ITranscriptService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[Transcript]: ...

    @abstractmethod
    def request(self, *, student, term, requested_by) -> Transcript: ...
