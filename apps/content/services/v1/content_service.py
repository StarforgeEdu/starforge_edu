"""Content application services (visibility-scoped CRUD + delegation to the
preserved file-upload / dual-approval / material domain functions)."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.content import selectors
from apps.content import services as domain
from apps.content.interfaces.repositories import (
    IContentLessonRepository,
    IContentLibraryRepository,
    ICourseRepository,
    IFolderRepository,
    ILessonFileRepository,
    ILibraryMaterialRepository,
    IModuleRepository,
)
from apps.content.interfaces.services import (
    IContentLessonService,
    IContentLibraryService,
    ICourseService,
    IFolderService,
    ILessonFileService,
    ILibraryMaterialService,
    IModuleService,
)
from apps.content.models import ContentLibrary, LessonFile, LibraryMaterial
from core.exceptions import PermissionException, ValidationException


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require_fk(model, value, field: str):
    if not model.objects.filter(pk=value).exists():
        raise _reject(field, f"{field} does not exist.")


def _assert_library_writable(library_id, *, actor, roles) -> None:
    """Content reads are visibility-scoped (DEPARTMENT/COHORT/ROLE walls); writes must be too.
    A ``content:write`` holder may only create/reparent content into a library they can SEE —
    otherwise they inject material into a restricted library, surfacing it to that library's
    members (cross-department/cohort content injection). ``actor=None`` is a trusted internal
    call (no HTTP actor) and skips the check; mirrors LibraryMaterialService.is_writable_library."""
    if actor is None or library_id is None:
        return
    if not selectors.scoped_libraries(user=actor, roles=roles).filter(pk=library_id).exists():
        raise PermissionException("You don't have access to that library.", code="library_out_of_scope")


def _course_library_id(course_id) -> int | None:
    from apps.content.models import Course

    return Course.objects.filter(pk=course_id).values_list("library_id", flat=True).first()


def _module_library_id(module_id) -> int | None:
    from apps.content.models import Module

    return Module.objects.filter(pk=module_id).values_list("course__library_id", flat=True).first()


class ContentLibraryService(IContentLibraryService):
    def __init__(self, repository: IContentLibraryRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None):
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        from apps.cohorts.models import Cohort
        from apps.org.models import Department

        out = {k: v for k, v in data.items() if k not in ("department", "cohort")}
        if "department" in data:
            if data["department"] is not None:
                _require_fk(Department, data["department"], "department")
            out["department_id"] = data["department"]
        if "cohort" in data:
            if data["cohort"] is not None:
                _require_fk(Cohort, data["cohort"], "cohort")
            out["cohort_id"] = data["cohort"]
        return out

    def create(self, *, data: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        # A library is the top-level container (no parent library to scope against); creating
        # one is a plain content:write authoring action. actor/roles accepted for a uniform
        # CRUD signature but not needed here.
        return self.repository.add(data=self._resolve(data))

    def update(self, obj, *, changes: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        return self.repository.apply_changes(obj, changes=self._resolve(changes))

    def delete(self, obj) -> None:
        self.repository.remove(obj)


class CourseService(ICourseService):
    def __init__(self, repository: ICourseRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None):
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        from apps.academics.models import Subject

        out = {k: v for k, v in data.items() if k not in ("library", "subject")}
        if "library" in data:
            _require_fk(ContentLibrary, data["library"], "library")
            out["library_id"] = data["library"]
        if "subject" in data:
            _require_fk(Subject, data["subject"], "subject")
            out["subject_id"] = data["subject"]
        return out

    def create(self, *, data: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(data)
        _assert_library_writable(resolved.get("library_id"), actor=actor, roles=roles)
        return self.repository.add(data=resolved)

    def update(self, obj, *, changes: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(changes)
        if "library_id" in resolved:  # reparent into a (possibly restricted) library
            _assert_library_writable(resolved["library_id"], actor=actor, roles=roles)
        return self.repository.apply_changes(obj, changes=resolved)

    def delete(self, obj) -> None:
        self.repository.remove(obj)


class ModuleService(IModuleService):
    def __init__(self, repository: IModuleRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None):
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        from apps.content.models import Course

        out = {k: v for k, v in data.items() if k != "course"}
        if "course" in data:
            _require_fk(Course, data["course"], "course")
            out["course_id"] = data["course"]
        return out

    def create(self, *, data: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(data)
        # Authorize the target library BEFORE probing order-taken (which would otherwise leak
        # whether an order is used in a course the actor cannot see).
        _assert_library_writable(_course_library_id(resolved.get("course_id")), actor=actor, roles=roles)
        if self.repository.order_taken(course_id=resolved["course_id"], order=resolved.get("order", 0)):
            raise _reject("order", "A module with this order already exists in the course.")
        return self.repository.add(data=resolved)

    def update(self, obj, *, changes: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(changes)
        if "course_id" in resolved:  # reparent into another course/library
            _assert_library_writable(_course_library_id(resolved["course_id"]), actor=actor, roles=roles)
        course_id = resolved.get("course_id", obj.course_id)
        if ("order" in resolved or "course_id" in resolved) and self.repository.order_taken(
            course_id=course_id, order=resolved.get("order", obj.order), exclude_pk=obj.pk
        ):
            raise _reject("order", "A module with this order already exists in the course.")
        return self.repository.apply_changes(obj, changes=resolved)

    def delete(self, obj) -> None:
        self.repository.remove(obj)


class ContentLessonService(IContentLessonService):
    def __init__(self, repository: IContentLessonRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None):
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        from apps.content.models import Module

        out = {k: v for k, v in data.items() if k != "module"}
        if "module" in data:
            _require_fk(Module, data["module"], "module")
            out["module_id"] = data["module"]
        return out

    def create(self, *, data: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(data)
        _assert_library_writable(_module_library_id(resolved.get("module_id")), actor=actor, roles=roles)
        return self.repository.add(data=resolved)

    def update(self, obj, *, changes: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(changes)
        if "module_id" in resolved:  # reparent into another module/course/library
            _assert_library_writable(_module_library_id(resolved["module_id"]), actor=actor, roles=roles)
        return self.repository.apply_changes(obj, changes=resolved)

    def delete(self, obj) -> None:
        self.repository.remove(obj)


class FolderService(IFolderService):
    def __init__(self, repository: IFolderRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None):
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def _resolve(self, data: dict[str, Any]) -> dict[str, Any]:
        from apps.content.models import Folder

        out = {k: v for k, v in data.items() if k not in ("library", "parent")}
        if "library" in data:
            _require_fk(ContentLibrary, data["library"], "library")
            out["library_id"] = data["library"]
        if "parent" in data:
            if data["parent"] is not None:
                _require_fk(Folder, data["parent"], "parent")
            out["parent_id"] = data["parent"]
        return out

    def create(self, *, data: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(data)
        _assert_library_writable(resolved.get("library_id"), actor=actor, roles=roles)
        if self.repository.name_taken(
            library_id=resolved["library_id"], parent_id=resolved.get("parent_id"), name=resolved["name"]
        ):
            raise _reject("name", "A folder with this name already exists here.")
        return self.repository.add(data=resolved)

    def update(self, obj, *, changes: dict[str, Any], actor: Any = None, roles: set[str] | None = None):
        resolved = self._resolve(changes)
        if "library_id" in resolved:  # reparent into a (possibly restricted) library
            _assert_library_writable(resolved["library_id"], actor=actor, roles=roles)
        library_id = resolved.get("library_id", obj.library_id)
        parent_id = resolved.get("parent_id", obj.parent_id)
        name = resolved.get("name", obj.name)
        if resolved and self.repository.name_taken(
            library_id=library_id, parent_id=parent_id, name=name, exclude_pk=obj.pk
        ):
            raise _reject("name", "A folder with this name already exists here.")
        return self.repository.apply_changes(obj, changes=resolved)

    def delete(self, obj) -> None:
        self.repository.remove(obj)


class LessonFileService(ILessonFileService):
    def __init__(self, repository: ILessonFileRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None) -> QuerySet[LessonFile]:
        return self.repository.scoped(user=user, roles=roles)

    def get_scoped(self, *, pk: int, user: Any, roles: set[str] | None) -> LessonFile | None:
        return self.repository.get_scoped(pk=pk, user=user, roles=roles)

    def request_upload(self, *, data: dict[str, Any], user) -> dict:
        return domain.request_upload(user=user, **data)

    def confirm(self, *, file: LessonFile) -> LessonFile:
        return domain.confirm_upload(file=file)

    def download_url(self, *, file: LessonFile, user, actor_is_staff: bool) -> dict:
        return domain.download_url(file=file, user=user, actor_is_staff=actor_is_staff)

    def track_view(self, *, file: LessonFile, user) -> None:
        domain.track_view(file=file, user=user)

    def new_version(self, *, previous: LessonFile, data: dict[str, Any], user) -> dict:
        return domain.create_new_version(previous=previous, user=user, **data)

    def approve_teacher(self, *, file: LessonFile, actor) -> LessonFile:
        return domain.approve_teacher_leg(file=file, actor=actor)

    def approve_manager(self, *, file: LessonFile, actor, actor_roles, is_downloadable) -> LessonFile:
        return domain.approve_manager_leg(
            file=file, actor=actor, actor_roles=actor_roles, is_downloadable=is_downloadable
        )


class LibraryMaterialService(ILibraryMaterialService):
    def __init__(self, repository: ILibraryMaterialRepository) -> None:
        self.repository = repository

    def scoped(self, *, user: Any, roles: set[str] | None, manages: bool) -> QuerySet[LibraryMaterial]:
        return self.repository.scoped(user=user, roles=roles, manages=manages)

    def get_scoped(
        self, *, pk: int, user: Any, roles: set[str] | None, manages: bool
    ) -> LibraryMaterial | None:
        return self.repository.get_scoped(pk=pk, user=user, roles=roles, manages=manages)

    def is_writable_library(self, *, library_id: int, user, roles: set[str] | None) -> bool:
        return selectors.scoped_libraries(user=user, roles=roles).filter(pk=library_id).exists()

    def create(self, *, library_id: int, title: str, topic: str, created_by) -> LibraryMaterial:
        library = ContentLibrary.objects.get(pk=library_id)
        return domain.create_material(library=library, title=title, topic=topic, created_by=created_by)

    def update(self, *, material: LibraryMaterial, fields: dict[str, Any]) -> LibraryMaterial:
        return domain.update_material(material_id=material.pk, fields=fields)

    def generate(self, *, material: LibraryMaterial, requested_by):
        return domain.request_material_generation(material=material, requested_by=requested_by)

    def publish(self, *, material: LibraryMaterial) -> LibraryMaterial:
        return domain.publish_material(material=material)
