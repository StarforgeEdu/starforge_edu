"""ORM-backed content repositories (reads scoped via the preserved selectors)."""

from __future__ import annotations

from typing import Any, ClassVar

from django.db.models import Q, QuerySet

from apps.content import selectors
from apps.content.interfaces.repositories import (
    IContentLessonRepository,
    IContentLibraryRepository,
    ICourseRepository,
    IFolderRepository,
    ILessonFileRepository,
    ILibraryMaterialRepository,
    IModuleRepository,
)
from apps.content.models import (
    ContentLesson,
    ContentLibrary,
    Course,
    Folder,
    LessonFile,
    LibraryMaterial,
    Module,
)
from core.repositories import BaseRepository


def _libs(user: Any, roles: set[str] | None, permission: str) -> QuerySet[ContentLibrary]:
    return selectors.scoped_libraries(user=user, roles=roles, permission=permission)


class _CrudRepo:
    """Shared create/update/delete for the visibility-scoped CRUD resources."""

    model: ClassVar[Any]

    def add(self, *, data: dict[str, Any]):
        return self.model.objects.create(**data)

    def apply_changes(self, obj, *, changes: dict[str, Any]):
        for field, value in changes.items():
            setattr(obj, field, value)
        if changes:
            obj.save(
                update_fields=[*changes.keys(), "updated_at"] if _has_updated(obj) else [*changes.keys()]
            )
        return obj

    def remove(self, obj) -> None:
        obj.delete()


def _has_updated(obj) -> bool:
    return any(f.name == "updated_at" for f in obj._meta.get_fields())


class ContentLibraryRepository(_CrudRepo, BaseRepository[ContentLibrary], IContentLibraryRepository):
    model = ContentLibrary

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[ContentLibrary]:
        return _libs(user, roles, permission)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None, permission: str):
        return _libs(user, roles, permission).filter(pk=pk).first()


class CourseRepository(_CrudRepo, BaseRepository[Course], ICourseRepository):
    model = Course

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[Course]:
        return Course.objects.filter(library__in=_libs(user, roles, permission)).select_related(
            "subject", "library"
        )

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None, permission: str):
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()


class ModuleRepository(_CrudRepo, BaseRepository[Module], IModuleRepository):
    model = Module

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[Module]:
        return Module.objects.filter(course__library__in=_libs(user, roles, permission)).select_related(
            "course"
        )

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None, permission: str):
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()

    def order_taken(self, *, course_id: int, order: int, exclude_pk: int | None = None) -> bool:
        qs = Module.objects.filter(course_id=course_id, order=order)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class ContentLessonRepository(_CrudRepo, BaseRepository[ContentLesson], IContentLessonRepository):
    model = ContentLesson

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[ContentLesson]:
        return ContentLesson.objects.filter(
            module__course__library__in=_libs(user, roles, permission)
        ).select_related("module")

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None, permission: str):
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()


class FolderRepository(_CrudRepo, BaseRepository[Folder], IFolderRepository):
    model = Folder

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[Folder]:
        return Folder.objects.filter(library__in=_libs(user, roles, permission)).select_related(
            "library", "parent"
        )

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None, permission: str):
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()

    def name_taken(
        self, *, library_id: int, parent_id: int | None, name: str, exclude_pk: int | None = None
    ) -> bool:
        qs = Folder.objects.filter(library_id=library_id, parent_id=parent_id, name=name)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        return qs.exists()


class LessonFileRepository(BaseRepository[LessonFile], ILessonFileRepository):
    model = LessonFile

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[LessonFile]:
        return selectors.scoped_files(user=user, roles=roles, permission=permission)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None, permission: str) -> LessonFile | None:
        return selectors.scoped_files(user=user, roles=roles, permission=permission).filter(pk=pk).first()


class LibraryMaterialRepository(BaseRepository[LibraryMaterial], ILibraryMaterialRepository):
    model = LibraryMaterial

    def scoped(self, *, user: Any, roles: set[str] | None, permission: str) -> QuerySet[LibraryMaterial]:
        qs = LibraryMaterial.objects.filter(library__in=_libs(user, roles, permission)).select_related(
            "library", "created_by"
        )
        if permission == "content:read":
            managed_scope = Q(pk__in=[])
            for management_permission in ("content:write", "content:approve", "content:publish"):
                managed_scope |= Q(pk__in=_libs(user, roles, management_permission).values("pk"))
            managed_library_ids = ContentLibrary.objects.filter(managed_scope).values("pk")
            qs = qs.filter(Q(status=LibraryMaterial.Status.PUBLISHED) | Q(library_id__in=managed_library_ids))
        return qs

    def get_scoped(
        self, *, pk: int, user: Any, roles: set[str] | None, permission: str
    ) -> LibraryMaterial | None:
        return self.scoped(user=user, roles=roles, permission=permission).filter(pk=pk).first()
