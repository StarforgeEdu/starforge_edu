"""Content-domain repository ports."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.content.models import (
    ContentLesson,
    ContentLibrary,
    Course,
    Folder,
    LessonFile,
    LibraryMaterial,
    Module,
)
from core.interfaces import IBaseRepository


class _ICrudRepository(IBaseRepository):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None):
        raise NotImplementedError

    def add(self, *, data: dict[str, Any]):
        raise NotImplementedError

    def apply_changes(self, obj, *, changes: dict[str, Any]):
        raise NotImplementedError

    def remove(self, obj) -> None:
        raise NotImplementedError


class IContentLibraryRepository(_ICrudRepository, IBaseRepository[ContentLibrary]):
    pass


class ICourseRepository(_ICrudRepository, IBaseRepository[Course]):
    pass


class IModuleRepository(_ICrudRepository, IBaseRepository[Module]):
    def order_taken(self, *, course_id: int, order: int, exclude_pk: int | None = None) -> bool:
        raise NotImplementedError


class IContentLessonRepository(_ICrudRepository, IBaseRepository[ContentLesson]):
    pass


class IFolderRepository(_ICrudRepository, IBaseRepository[Folder]):
    def name_taken(
        self, *, library_id: int, parent_id: int | None, name: str, exclude_pk: int | None = None
    ) -> bool:
        raise NotImplementedError


class ILessonFileRepository(IBaseRepository[LessonFile]):
    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[LessonFile]:
        raise NotImplementedError

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> LessonFile | None:
        raise NotImplementedError


class ILibraryMaterialRepository(IBaseRepository[LibraryMaterial]):
    def scoped(self, *, user: Any, roles: set[str] | None, manages: bool) -> QuerySet[LibraryMaterial]:
        raise NotImplementedError

    def get_scoped(
        self, *, pk: int, user: Any, roles: set[str] | None, manages: bool
    ) -> LibraryMaterial | None:
        raise NotImplementedError
