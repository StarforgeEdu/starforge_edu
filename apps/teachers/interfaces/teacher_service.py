"""Teacher service port — the contract the views resolve from the container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.teachers.dto.teacher_dto import TeacherCreateDTO
from apps.teachers.models import TeacherProfile


class ITeacherService(ABC):
    @abstractmethod
    def list(self) -> QuerySet[TeacherProfile]:
        """Base (unscoped) queryset with relations eager-loaded; the view scopes +
        filters + paginates it."""

    @abstractmethod
    def get(self, teacher_id: int) -> TeacherProfile | None: ...

    @abstractmethod
    def create(self, data: TeacherCreateDTO) -> TeacherProfile: ...

    @abstractmethod
    def update(self, teacher: TeacherProfile, changes: dict[str, Any]) -> TeacherProfile:
        """Apply the provided fields (PATCH-style) with the branch↔department guard."""

    @abstractmethod
    def delete(self, teacher: TeacherProfile) -> None: ...

    @abstractmethod
    def dashboard(self, user, roles) -> dict[str, Any]: ...
