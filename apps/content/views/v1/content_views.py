"""Content HTTP views (layered, off DRF).

Visibility-scoped CRUD for libraries/courses/modules/lessons/folders, the signed-URL
file upload+download flow with the F4-5 dual (teacher then manager) publication
approval, and AI-drafted library materials (draft -> generate -> edit -> publish).
"""

from __future__ import annotations

import re
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.content.interfaces.services import (
    IContentLessonService,
    IContentLibraryService,
    ICourseService,
    IFolderService,
    ILessonFileService,
    ILibraryMaterialService,
    IModuleService,
)
from apps.content.models import ContentLesson, ContentLibrary, Folder, LibraryMaterial
from apps.content.presenters import (
    course_to_dict,
    folder_to_dict,
    lesson_file_to_dict,
    lesson_to_dict,
    library_to_dict,
    material_to_dict,
    module_to_dict,
)
from apps.content.selectors import REVIEWER_ROLES, scoped_libraries
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, PermissionException, ValidationException
from core.http import bool_field, parse_bool, read_json
from core.listing import apply_filters, paginate
from core.permissions import get_user_roles, has_permission_code
from core.responses import created, error, no_content, paginated, success

# --- service accessors -----------------------------------------------------


def _library_service() -> IContentLibraryService:
    return container.resolve(IContentLibraryService)  # type: ignore[type-abstract]


def _course_service() -> ICourseService:
    return container.resolve(ICourseService)  # type: ignore[type-abstract]


def _module_service() -> IModuleService:
    return container.resolve(IModuleService)  # type: ignore[type-abstract]


def _lesson_service() -> IContentLessonService:
    return container.resolve(IContentLessonService)  # type: ignore[type-abstract]


def _folder_service() -> IFolderService:
    return container.resolve(IFolderService)  # type: ignore[type-abstract]


def _file_service() -> ILessonFileService:
    return container.resolve(ILessonFileService)  # type: ignore[type-abstract]


def _material_service() -> ILibraryMaterialService:
    return container.resolve(ILibraryMaterialService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _roles(request: HttpRequest) -> set[str]:
    return get_user_roles(request)


def _manages_content(request: HttpRequest) -> bool:
    if request.user.is_superuser:
        return True
    roles = _roles(request)
    return any(has_permission_code(roles, c) for c in ("content:write", "content:approve", "content:publish"))


# --- value validators (never-500) ------------------------------------------

_FILENAME_ALLOWED = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,254}$")


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_value(raw: Any, name: str, *, max_length: int | None = None, allow_blank: bool = False) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    value = raw.strip()
    if "\x00" in value:
        raise _reject(name, "Null characters are not allowed.")
    if not value and not allow_blank:
        raise _reject(name, "This field may not be blank.")
    if max_length is not None and len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _int_value(raw: Any, name: str, *, min_value: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise _reject(name, "A valid integer is required.")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise _reject(name, "A valid integer is required.") from None
    if min_value is not None and value < min_value:
        raise _reject(name, f"Ensure this value is greater than or equal to {min_value}.")
    return value


def _choice_value(raw: Any, name: str, choices) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(choices)}.")
    return raw


def _str_list_value(raw: Any, name: str) -> list[str]:
    if not isinstance(raw, list):
        raise _reject(name, "Must be a list.")
    for element in raw:
        if not isinstance(element, str):
            raise _reject(name, "Each item must be a string.")
    return raw


def _sanitize_filename(raw: Any, name: str = "filename") -> str:
    """Reject a client filename that is not a safe basename (it flows into an S3 key)."""
    value = _str_value(raw, name, max_length=255)
    if value in {".", ".."} or value.startswith(".") or not _FILENAME_ALLOWED.match(value):
        raise _reject(
            name, "Filename must be a safe basename (letters/digits/._- , no path separators or leading dot)."
        )
    return value


# --- generic CRUD collection/detail helpers --------------------------------


def _crud_collection(
    request, service, presenter, *, filter_fields, search_fields, default_ordering, create_data_fn
):
    if request.method in ("GET", "HEAD"):
        check_perm(request, "content:read")
        qs = apply_filters(
            request,
            service.scoped(user=request.user, roles=_roles(request)),
            filter_fields=filter_fields,
            search_fields=search_fields,
            default_ordering=default_ordering,
        )
        items, total, page, size = paginate(request, qs)
        return paginated([presenter(o) for o in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "content:write")
        # Scope the write like the read: verify the target library is visible to the caller
        # (services enforce this when actor/roles are passed), closing the read/write asymmetry.
        obj = service.create(data=create_data_fn(request), actor=request.user, roles=_roles(request))
        return created(presenter(obj))
    return _method_not_allowed()


def _crud_detail(request, pk, service, presenter, *, create_data_fn, changes_fn):
    read = request.method in ("GET", "HEAD")
    check_perm(request, "content:read" if read else "content:write")
    obj = service.get_scoped(pk=pk, user=request.user, roles=_roles(request))
    if obj is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(presenter(obj))
    actor, roles = request.user, _roles(request)
    if request.method == "PUT":
        # Full replace (DRF parity): all required fields must be present, or 400.
        return success(presenter(service.update(obj, changes=create_data_fn(request), actor=actor, roles=roles)))
    if request.method == "PATCH":
        return success(presenter(service.update(obj, changes=changes_fn(request), actor=actor, roles=roles)))
    if request.method == "DELETE":
        service.delete(obj)
        return no_content()
    return _method_not_allowed()


# --- libraries -------------------------------------------------------------

_VISIBILITY = set(ContentLibrary.Visibility.values)


def _library_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {
        "name": _str_value(_require(data, "name"), "name", max_length=200),
        "description": _str_value(data.get("description", ""), "description", allow_blank=True),
        "visibility": _choice_value(data.get("visibility", "tenant"), "visibility", _VISIBILITY),
        "allowed_roles": _str_list_value(data.get("allowed_roles", []), "allowed_roles"),
        "is_active": bool_field(data, "is_active", default=True),
        "department": None
        if data.get("department") is None
        else _int_value(data["department"], "department"),
        "cohort": None if data.get("cohort") is None else _int_value(data["cohort"], "cohort"),
    }
    return out


def _library_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=200)
    if "description" in data:
        changes["description"] = _str_value(data["description"], "description", allow_blank=True)
    if "visibility" in data:
        changes["visibility"] = _choice_value(data["visibility"], "visibility", _VISIBILITY)
    if "allowed_roles" in data:
        changes["allowed_roles"] = _str_list_value(data["allowed_roles"], "allowed_roles")
    if "is_active" in data:
        changes["is_active"] = parse_bool(data["is_active"], "is_active")
    if "department" in data:
        changes["department"] = (
            None if data["department"] is None else _int_value(data["department"], "department")
        )
    if "cohort" in data:
        changes["cohort"] = None if data["cohort"] is None else _int_value(data["cohort"], "cohort")
    return changes


@csrf_exempt
@require_auth
def libraries_collection_view(request: HttpRequest) -> HttpResponse:
    return _crud_collection(
        request,
        _library_service(),
        library_to_dict,
        filter_fields=("visibility", "department", "cohort", "is_active"),
        search_fields=("name",),
        default_ordering="name",
        create_data_fn=_library_create_data,
    )


@csrf_exempt
@require_auth
def library_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _crud_detail(
        request,
        pk,
        _library_service(),
        library_to_dict,
        create_data_fn=_library_create_data,
        changes_fn=_library_changes,
    )


# --- courses ---------------------------------------------------------------


def _course_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "library": _int_value(_require(data, "library"), "library"),
        "subject": _int_value(_require(data, "subject"), "subject"),
        "title": _str_value(_require(data, "title"), "title", max_length=200),
        "description": _str_value(data.get("description", ""), "description", allow_blank=True),
        "order": _int_value(data.get("order", 0), "order", min_value=0),
    }


def _course_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "library" in data:
        changes["library"] = _int_value(data["library"], "library")
    if "subject" in data:
        changes["subject"] = _int_value(data["subject"], "subject")
    if "title" in data:
        changes["title"] = _str_value(data["title"], "title", max_length=200)
    if "description" in data:
        changes["description"] = _str_value(data["description"], "description", allow_blank=True)
    if "order" in data:
        changes["order"] = _int_value(data["order"], "order", min_value=0)
    return changes


@csrf_exempt
@require_auth
def courses_collection_view(request: HttpRequest) -> HttpResponse:
    return _crud_collection(
        request,
        _course_service(),
        course_to_dict,
        filter_fields=("library", "subject"),
        search_fields=("title",),
        default_ordering=None,
        create_data_fn=_course_create_data,
    )


@csrf_exempt
@require_auth
def course_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _crud_detail(
        request,
        pk,
        _course_service(),
        course_to_dict,
        create_data_fn=_course_create_data,
        changes_fn=_course_changes,
    )


# --- modules ---------------------------------------------------------------


def _module_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "course": _int_value(_require(data, "course"), "course"),
        "title": _str_value(_require(data, "title"), "title", max_length=200),
        "order": _int_value(data.get("order", 0), "order", min_value=0),
    }


def _module_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "course" in data:
        changes["course"] = _int_value(data["course"], "course")
    if "title" in data:
        changes["title"] = _str_value(data["title"], "title", max_length=200)
    if "order" in data:
        changes["order"] = _int_value(data["order"], "order", min_value=0)
    return changes


@csrf_exempt
@require_auth
def modules_collection_view(request: HttpRequest) -> HttpResponse:
    return _crud_collection(
        request,
        _module_service(),
        module_to_dict,
        filter_fields=("course",),
        search_fields=(),
        default_ordering=None,
        create_data_fn=_module_create_data,
    )


@csrf_exempt
@require_auth
def module_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _crud_detail(
        request,
        pk,
        _module_service(),
        module_to_dict,
        create_data_fn=_module_create_data,
        changes_fn=_module_changes,
    )


# --- content lessons -------------------------------------------------------


def _lesson_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "module": _int_value(_require(data, "module"), "module"),
        "title": _str_value(_require(data, "title"), "title", max_length=200),
        "description": _str_value(data.get("description", ""), "description", allow_blank=True),
        "order": _int_value(data.get("order", 0), "order", min_value=0),
    }


def _lesson_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "module" in data:
        changes["module"] = _int_value(data["module"], "module")
    if "title" in data:
        changes["title"] = _str_value(data["title"], "title", max_length=200)
    if "description" in data:
        changes["description"] = _str_value(data["description"], "description", allow_blank=True)
    if "order" in data:
        changes["order"] = _int_value(data["order"], "order", min_value=0)
    return changes


@csrf_exempt
@require_auth
def lessons_collection_view(request: HttpRequest) -> HttpResponse:
    return _crud_collection(
        request,
        _lesson_service(),
        lesson_to_dict,
        filter_fields=("module",),
        search_fields=("title",),
        default_ordering=None,
        create_data_fn=_lesson_create_data,
    )


@csrf_exempt
@require_auth
def lesson_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _crud_detail(
        request,
        pk,
        _lesson_service(),
        lesson_to_dict,
        create_data_fn=_lesson_create_data,
        changes_fn=_lesson_changes,
    )


# --- folders ---------------------------------------------------------------


def _folder_create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "library": _int_value(_require(data, "library"), "library"),
        "parent": None if data.get("parent") is None else _int_value(data["parent"], "parent"),
        "name": _str_value(_require(data, "name"), "name", max_length=200),
    }


def _folder_changes(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "library" in data:
        changes["library"] = _int_value(data["library"], "library")
    if "parent" in data:
        changes["parent"] = None if data["parent"] is None else _int_value(data["parent"], "parent")
    if "name" in data:
        changes["name"] = _str_value(data["name"], "name", max_length=200)
    return changes


@csrf_exempt
@require_auth
def folders_collection_view(request: HttpRequest) -> HttpResponse:
    return _crud_collection(
        request,
        _folder_service(),
        folder_to_dict,
        filter_fields=("library", "parent"),
        search_fields=(),
        default_ordering=None,
        create_data_fn=_folder_create_data,
    )


@csrf_exempt
@require_auth
def folder_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    return _crud_detail(
        request,
        pk,
        _folder_service(),
        folder_to_dict,
        create_data_fn=_folder_create_data,
        changes_fn=_folder_changes,
    )


# --- lesson files (read + signed-URL upload/download + F4-5 approvals) ------


@csrf_exempt
@require_auth
def files_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "content:read")
        qs = apply_filters(
            request,
            _file_service().scoped(user=request.user, roles=_roles(request)),
            filter_fields=("status", "lesson", "folder"),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([lesson_file_to_dict(f) for f in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        # Files are created only via /upload-url/ or /new-version/, never a bare POST.
        check_perm(request, "content:write")
        return error("Upload via /content/upload-url/.", code="method_not_allowed", status=405)
    return _method_not_allowed()


def _get_file_in_scope(request: HttpRequest, pk: int):
    f = _file_service().get_scoped(pk=pk, user=request.user, roles=_roles(request))
    if f is None:
        raise NotFoundException(code="not_found")
    return f


@csrf_exempt
@require_auth
def file_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "content:read")
    return success(lesson_file_to_dict(_get_file_in_scope(request, pk)))


@csrf_exempt
@require_auth
def file_confirm_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:write")
    _file_service().confirm(file=_get_file_in_scope(request, pk))
    return success({"status": "pending"}, status=202)


@csrf_exempt
@require_auth
def file_download_url_view(request: HttpRequest, pk: int) -> HttpResponse:
    # GET only (mirrors the old @action(methods=["get"])): download_url has write
    # side-effects (download_count + a FileView row), so a HEAD must NOT reach it.
    if request.method != "GET":
        return _method_not_allowed()
    check_perm(request, "content:read")
    file = _get_file_in_scope(request, pk)
    is_staff = request.user.is_superuser or bool(_roles(request) & REVIEWER_ROLES)
    return success(_file_service().download_url(file=file, user=request.user, actor_is_staff=is_staff))


@csrf_exempt
@require_auth
def file_track_view_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:read")
    _file_service().track_view(file=_get_file_in_scope(request, pk), user=request.user)
    return no_content()


def _upload_payload(result: dict) -> dict:
    return {
        "file_id": result["file"].id,
        "url": result["url"],
        "key": result["key"],
        "expires_in": result["expires_in"],
    }


@csrf_exempt
@require_auth
def file_new_version_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:write")
    previous = _get_file_in_scope(request, pk)
    data = read_json(request)
    payload = {
        "filename": _sanitize_filename(_require(data, "filename")),
        "content_type": _str_value(_require(data, "content_type"), "content_type", max_length=127),
        "size_bytes": _int_value(_require(data, "size_bytes"), "size_bytes", min_value=1),
    }
    result = _file_service().new_version(previous=previous, data=payload, user=request.user)
    return success(_upload_payload(result))


@csrf_exempt
@require_auth
def file_approve_teacher_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:approve")
    file = _file_service().approve_teacher(file=_get_file_in_scope(request, pk), actor=request.user)
    return success(lesson_file_to_dict(file))


@csrf_exempt
@require_auth
def file_approve_manager_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:publish")
    file = _get_file_in_scope(request, pk)
    data = read_json(request)
    is_downloadable = (
        None if "is_downloadable" not in data else parse_bool(data["is_downloadable"], "is_downloadable")
    )
    file = _file_service().approve_manager(
        file=file, actor=request.user, actor_roles=_roles(request), is_downloadable=is_downloadable
    )
    return success(lesson_file_to_dict(file))


@csrf_exempt
@require_auth
def content_upload_url_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:write")
    data = read_json(request)
    filename = _sanitize_filename(_require(data, "filename"))
    content_type = _str_value(_require(data, "content_type"), "content_type", max_length=127)
    size_bytes = _int_value(_require(data, "size_bytes"), "size_bytes", min_value=1)
    title = _str_value(data.get("title", ""), "title", max_length=255, allow_blank=True)
    # Writes are scoped like reads: a writer may only attach into a lesson/folder whose
    # library they can see (an out-of-scope pk -> 400, closing the read/write asymmetry).
    libs = scoped_libraries(user=request.user, roles=_roles(request))
    lesson = folder = None
    if data.get("lesson") is not None:
        lesson = ContentLesson.objects.filter(
            module__course__library__in=libs, pk=_int_value(data["lesson"], "lesson")
        ).first()
        if lesson is None:
            raise _reject("lesson", "lesson does not exist.")
    if data.get("folder") is not None:
        folder = Folder.objects.filter(library__in=libs, pk=_int_value(data["folder"], "folder")).first()
        if folder is None:
            raise _reject("folder", "folder does not exist.")
    if lesson is None and folder is None:
        raise _reject("lesson", "A file must be attached to a lesson or a folder.")
    payload = {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "title": title,
        "lesson": lesson,
        "folder": folder,
    }
    result = _file_service().request_upload(data=payload, user=request.user)
    return success(_upload_payload(result))


# --- library materials (AI-drafted, human-published) -----------------------


@csrf_exempt
@require_auth
def materials_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "content:read")
        qs = apply_filters(
            request,
            _material_service().scoped(
                user=request.user, roles=_roles(request), manages=_manages_content(request)
            ),
            filter_fields=("library", "status"),
            search_fields=("title",),
            default_ordering="-created_at",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([material_to_dict(m) for m in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "content:write")
        data = read_json(request)
        library_id = _int_value(_require(data, "library"), "library")
        title = _str_value(_require(data, "title"), "title", max_length=200)
        topic = _str_value(data.get("topic", ""), "topic", max_length=500, allow_blank=True)
        if not ContentLibrary.objects.filter(pk=library_id).exists():
            raise _reject("library", "library does not exist.")
        if not _material_service().is_writable_library(
            library_id=library_id, user=request.user, roles=_roles(request)
        ):
            raise PermissionException(
                "You can only add materials to a library you can access.", code="library_out_of_scope"
            )
        material = _material_service().create(
            library_id=library_id, title=title, topic=topic, created_by=request.user
        )
        return created(material_to_dict(material))
    return _method_not_allowed()


def _get_material_in_scope(request: HttpRequest, pk: int, *, manages: bool) -> LibraryMaterial:
    material = _material_service().get_scoped(
        pk=pk, user=request.user, roles=_roles(request), manages=manages
    )
    if material is None:
        raise NotFoundException(code="not_found")
    return material


@csrf_exempt
@require_auth
def material_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "content:read")
        material = _get_material_in_scope(request, pk, manages=_manages_content(request))
        return success(material_to_dict(material))
    if request.method == "PATCH":
        check_perm(request, "content:write")
        material = _get_material_in_scope(request, pk, manages=True)
        data = read_json(request)
        fields: dict[str, Any] = {}
        if "title" in data:
            fields["title"] = _str_value(data["title"], "title", max_length=200)
        if "topic" in data:
            fields["topic"] = _str_value(data["topic"], "topic", max_length=500, allow_blank=True)
        if "body" in data:
            fields["body"] = _str_value(data["body"], "body", allow_blank=True)
        material = _material_service().update(material=material, fields=fields)
        return success(material_to_dict(material))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def material_generate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:write")
    material = _get_material_in_scope(request, pk, manages=True)
    ai_request = _material_service().generate(material=material, requested_by=request.user)
    return success({"request_id": ai_request.pk, "status": ai_request.status}, status=202)


@csrf_exempt
@require_auth
def material_publish_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "content:publish")
    material = _get_material_in_scope(request, pk, manages=True)
    return success(material_to_dict(_material_service().publish(material=material)))
