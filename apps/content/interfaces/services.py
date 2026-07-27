"""Content-domain service ports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.content.models import LessonFile, LibraryMaterial


class _ICrudService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet: ...

    @abstractmethod
    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None): ...

    @abstractmethod
    def create(self, *, data: dict[str, Any], actor: Any = None, roles: set[str] | None = None): ...

    @abstractmethod
    def update(
        self, obj, *, changes: dict[str, Any], actor: Any = None, roles: set[str] | None = None
    ): ...

    @abstractmethod
    def delete(self, obj) -> None: ...


class IContentLibraryService(_ICrudService): ...


class ICourseService(_ICrudService): ...


class IModuleService(_ICrudService): ...


class IContentLessonService(_ICrudService): ...


class IFolderService(_ICrudService): ...


class ILessonFileService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[LessonFile]: ...

    @abstractmethod
    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> LessonFile | None: ...

    @abstractmethod
    def request_upload(self, *, data: dict[str, Any], user) -> dict: ...

    @abstractmethod
    def confirm(self, *, file: LessonFile) -> LessonFile: ...

    @abstractmethod
    def download_url(self, *, file: LessonFile, user, actor_is_staff: bool) -> dict: ...

    @abstractmethod
    def track_view(self, *, file: LessonFile, user) -> None: ...

    @abstractmethod
    def new_version(self, *, previous: LessonFile, data: dict[str, Any], user) -> dict: ...

    @abstractmethod
    def approve_teacher(self, *, file: LessonFile, actor) -> LessonFile: ...

    @abstractmethod
    def approve_manager(self, *, file: LessonFile, actor, actor_roles, is_downloadable) -> LessonFile: ...


class ILibraryMaterialService(ABC):
    @abstractmethod
    def scoped(self, *, user: Any, roles: set[str] | None, manages: bool) -> QuerySet[LibraryMaterial]: ...

    @abstractmethod
    def get_scoped(
        self, *, pk: int, user: Any, roles: set[str] | None, manages: bool
    ) -> LibraryMaterial | None: ...

    @abstractmethod
    def is_writable_library(self, *, library_id: int, user, roles: set[str] | None) -> bool: ...

    @abstractmethod
    def create(self, *, library_id: int, title: str, topic: str, created_by) -> LibraryMaterial: ...

    @abstractmethod
    def update(self, *, material: LibraryMaterial, fields: dict[str, Any]) -> LibraryMaterial: ...

    @abstractmethod
    def generate(self, *, material: LibraryMaterial, requested_by): ...

    @abstractmethod
    def publish(self, *, material: LibraryMaterial) -> LibraryMaterial: ...
