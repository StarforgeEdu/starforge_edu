"""Assignment-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.assignments.dto.assignment_dto import CreateAssignmentDTO
from apps.assignments.models import Assignment, Submission, SubmissionGrade


class IAssignmentService(ABC):
    @abstractmethod
    def scoped_list(
        self,
        *,
        user,
        roles: set[str],
        permission: str = "assignments:read",
    ) -> QuerySet[Assignment]: ...

    @abstractmethod
    def get_visible(
        self,
        *,
        user,
        roles: set[str],
        pk: int,
        permission: str = "assignments:read",
    ) -> Assignment | None: ...

    @abstractmethod
    def create(self, data: CreateAssignmentDTO, *, creator, user, roles: set[str]) -> Assignment: ...

    @abstractmethod
    def update(
        self, assignment: Assignment, changes: dict[str, Any], *, user, roles: set[str]
    ) -> Assignment: ...

    @abstractmethod
    def delete(self, assignment: Assignment, *, user, roles: set[str]) -> None: ...

    @abstractmethod
    def publish(self, assignment: Assignment, *, actor) -> Assignment: ...

    @abstractmethod
    def close(self, assignment: Assignment, *, actor) -> Assignment: ...

    @abstractmethod
    def submissions_of(
        self,
        assignment: Assignment,
        *,
        user,
        roles: set[str],
        permission: str = "assignments:read",
    ) -> QuerySet[Submission]: ...

    @abstractmethod
    def submit(
        self, assignment: Assignment, *, student, text: str, attachment_keys: list, actor=None
    ) -> Submission: ...

    @abstractmethod
    def upload_url(
        self, *, filename: str, content_type: str, size_bytes: int, requested_by
    ) -> dict[str, Any]: ...


class ISubmissionService(ABC):
    @abstractmethod
    def scoped_list(
        self,
        *,
        user,
        roles: set[str],
        permission: str = "assignments:read",
    ) -> QuerySet[Submission]: ...

    @abstractmethod
    def get_visible(
        self,
        *,
        user,
        roles: set[str],
        pk: int,
        permission: str = "assignments:read",
    ) -> Submission | None: ...

    @abstractmethod
    def grade(
        self, submission: Submission, *, score, rubric_scores: list, feedback: str, actor
    ) -> SubmissionGrade: ...

    @abstractmethod
    def return_for_revision(self, submission: Submission, *, actor) -> Submission: ...

    @abstractmethod
    def check_plagiarism(self, submission: Submission): ...

    @abstractmethod
    def request_ai_feedback(
        self,
        submission: Submission,
        *,
        requested_by,
        requested_principal=None,
    ) -> None: ...
