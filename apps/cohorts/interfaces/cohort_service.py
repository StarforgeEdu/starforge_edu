"""Cohort service port — the contract the views resolve from the container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.cohorts.dto.cohort_dto import (
    CohortCreateDTO,
    CohortEnrollDTO,
    CohortMoveDTO,
    CohortRemoveDTO,
    CohortTeacherDTO,
    TeacherTypeCreateDTO,
)
from apps.cohorts.models import Cohort, CohortMembership, CohortTeacher
from apps.teachers.models import TeacherType


class ICohortService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[Cohort]:
        """Base (unscoped) queryset with relations eager-loaded; the view scopes +
        filters + paginates it."""

    @abstractmethod
    def get(self, cohort_id: int) -> Cohort | None: ...

    @abstractmethod
    def create(self, data: CohortCreateDTO) -> Cohort: ...

    @abstractmethod
    def update(self, cohort: Cohort, changes: dict[str, Any]) -> Cohort:
        """Apply the provided fields (PATCH-style); archived cohorts are read-only."""

    @abstractmethod
    def delete(self, cohort: Cohort) -> None:
        """Hard-delete only an empty, unarchived cohort; else 400/409 (history is kept)."""

    @abstractmethod
    def unarchive(self, cohort: Cohort) -> Cohort: ...

    @abstractmethod
    def enroll(self, cohort: Cohort, data: CohortEnrollDTO) -> CohortMembership: ...

    @abstractmethod
    def move(self, cohort: Cohort, data: CohortMoveDTO, actor) -> dict[str, Any]:
        """Move a student into ``cohort`` (history preserved); returns
        {membership, over_capacity}."""

    @abstractmethod
    def remove_member(self, cohort: Cohort, data: CohortRemoveDTO, actor) -> CohortMembership:
        """Remove a student from ``cohort`` without moving them (groupless); history
        preserved. Returns the end-dated membership."""

    @abstractmethod
    def members(self, cohort: Cohort) -> QuerySet[CohortMembership]: ...

    @abstractmethod
    def teacher_types(self) -> QuerySet[TeacherType]: ...

    @abstractmethod
    def get_teacher_type(self, teacher_type_id: int) -> TeacherType | None: ...

    @abstractmethod
    def create_teacher_type(self, data: TeacherTypeCreateDTO) -> TeacherType: ...

    @abstractmethod
    def update_teacher_type(self, teacher_type: TeacherType, changes: dict[str, Any]) -> TeacherType: ...

    @abstractmethod
    def delete_teacher_type(self, teacher_type: TeacherType) -> None: ...

    @abstractmethod
    def co_teachers(self, cohort: Cohort) -> QuerySet[CohortTeacher]:
        """The cohort's canonical typed teacher roster."""

    @abstractmethod
    def get_teacher_assignment(self, cohort: Cohort, assignment_id: int) -> CohortTeacher | None: ...

    @abstractmethod
    def assign_teacher(self, cohort: Cohort, data: CohortTeacherDTO) -> tuple[CohortTeacher, bool]:
        """Assign a teacher/type triple idempotently; returns (row, created)."""

    @abstractmethod
    def update_teacher_assignment(
        self, cohort: Cohort, assignment: CohortTeacher, changes: dict[str, Any]
    ) -> CohortTeacher: ...

    @abstractmethod
    def remove_teacher(self, cohort: Cohort, assignment: CohortTeacher) -> None:
        """Delete exactly one typed assignment."""
