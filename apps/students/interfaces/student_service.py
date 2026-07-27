"""Student service port — the contract the views resolve from the container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.students.dto.student_dto import StudentCreateDTO, TransitionDTO
from apps.students.models import EnrollmentEvent, EnrollmentReason, StudentProfile


class IEnrollmentReasonService(ABC):
    @abstractmethod
    def list_reasons(self) -> QuerySet[EnrollmentReason]: ...

    @abstractmethod
    def get(self, *, pk: int) -> EnrollmentReason | None: ...

    @abstractmethod
    def create(self, *, data: dict[str, Any]) -> EnrollmentReason: ...

    @abstractmethod
    def update(self, reason: EnrollmentReason, *, changes: dict[str, Any]) -> EnrollmentReason: ...

    @abstractmethod
    def delete(self, reason: EnrollmentReason) -> None: ...

    @abstractmethod
    def active_slugs(self) -> set[str]: ...


class IStudentService(ABC):
    # --- CRUD --------------------------------------------------------------
    @abstractmethod
    def scoped_list(self, *, user, roles) -> QuerySet[StudentProfile]: ...

    @abstractmethod
    def get(self, *, user, roles, pk: int) -> StudentProfile | None: ...

    @abstractmethod
    def create(self, data: StudentCreateDTO) -> StudentProfile: ...

    @abstractmethod
    def update(self, student: StudentProfile, changes: dict[str, Any]) -> StudentProfile: ...

    @abstractmethod
    def delete(self, student: StudentProfile) -> None: ...

    # --- detail actions ----------------------------------------------------
    @abstractmethod
    def transition(self, student: StudentProfile, data: TransitionDTO, actor) -> StudentProfile: ...

    @abstractmethod
    def block(self, student: StudentProfile, reason: str, actor) -> StudentProfile: ...

    @abstractmethod
    def unblock(self, student: StudentProfile, actor) -> StudentProfile: ...

    @abstractmethod
    def events(self, student: StudentProfile) -> QuerySet[EnrollmentEvent]: ...

    @abstractmethod
    def issue_credentials(self, student: StudentProfile, *, actor) -> dict[str, Any]:
        """Issue a one-time login password for the student (temp + force-change on first
        login). Returns {username, temporary_password} — shown once."""

    # --- collection actions ------------------------------------------------
    @abstractmethod
    def import_csv(self, *, file_obj, branch_id: int) -> dict[str, Any]: ...

    @abstractmethod
    def birthdays(self, *, user, roles, days: int, branch, cohort) -> QuerySet[StudentProfile]: ...

    @abstractmethod
    def stats(self, *, user, roles) -> dict[str, Any]: ...

    @abstractmethod
    def comparison(self, *, user, roles, metric: str, unit: str) -> dict[str, Any]: ...

    # --- self-service ------------------------------------------------------
    @abstractmethod
    def require_profile(self, user) -> StudentProfile:
        """The caller's own student profile, or raise 404 not_a_student."""

    @abstractmethod
    def dashboard(self, *, user, roles) -> dict[str, Any]: ...

    @abstractmethod
    def report(self, *, user) -> dict[str, Any]: ...
