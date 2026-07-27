"""Content response presenters (the DRF serializer output shapes)."""

from __future__ import annotations

from apps.content.models import (
    ContentLesson,
    ContentLibrary,
    Course,
    Folder,
    LessonFile,
    LibraryMaterial,
    Module,
)


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def library_to_dict(lib: ContentLibrary) -> dict:
    # Each bare FK id keeps a readable `_name` companion so a client renders the
    # library without a second call. The list queryset (scoped_libraries)
    # select_relateds department + cohort, so these add no query per row.
    return {
        "id": lib.id,
        "name": lib.name,
        "description": lib.description,
        "visibility": lib.visibility,
        "department": lib.department_id,
        "department_name": lib.department.name if lib.department else None,
        "cohort": lib.cohort_id,
        "cohort_name": lib.cohort.name if lib.cohort else None,
        "allowed_roles": lib.allowed_roles,
        "is_active": lib.is_active,
    }


def course_to_dict(course: Course) -> dict:
    # `library`/`subject` are non-null FKs — surface their readable labels beside the
    # ids. The courses list queryset select_relateds library + subject (no N+1).
    return {
        "id": course.id,
        "library": course.library_id,
        "library_name": course.library.name,
        "subject": course.subject_id,
        "subject_name": course.subject.name,
        "title": course.title,
        "description": course.description,
        "order": course.order,
    }


def module_to_dict(module: Module) -> dict:
    # `course` is a non-null FK; the modules list queryset select_relateds it.
    return {
        "id": module.id,
        "course": module.course_id,
        "course_title": module.course.title,
        "title": module.title,
        "order": module.order,
    }


def lesson_to_dict(lesson: ContentLesson) -> dict:
    # `module` is a non-null FK; the lessons list queryset select_relateds it.
    return {
        "id": lesson.id,
        "module": lesson.module_id,
        "module_title": lesson.module.title,
        "title": lesson.title,
        "description": lesson.description,
        "order": lesson.order,
    }


def folder_to_dict(folder: Folder) -> dict:
    # `library` (non-null) + `parent` (nullable self-FK) get readable companions;
    # the folders list queryset select_relateds library + parent (no N+1).
    return {
        "id": folder.id,
        "library": folder.library_id,
        "library_name": folder.library.name,
        "parent": folder.parent_id,
        "parent_name": folder.parent.name if folder.parent else None,
        "name": folder.name,
    }


def lesson_file_to_dict(f: LessonFile) -> dict:
    thumbnail_url = None
    if f.thumbnail_key:
        from infrastructure.storage.s3_client import presign_download

        thumbnail_url = presign_download(f.thumbnail_key, expires_in=300)
    return {
        "id": f.id,
        "lesson": f.lesson_id,
        # `lesson`/`folder`/`uploaded_by` get readable companions so a file row is
        # self-describing; scoped_files already select_relateds all three (no N+1).
        "lesson_title": f.lesson.title if f.lesson else None,
        "folder": f.folder_id,
        "folder_name": f.folder.name if f.folder else None,
        "title": f.title,
        "content_type": f.content_type,
        "size_bytes": f.size_bytes,
        "status": f.status,
        "reject_reason": f.reject_reason,
        "version": f.version,
        "previous_version": f.previous_version_id,
        "thumbnail_url": thumbnail_url,
        "view_count": f.view_count,
        "download_count": f.download_count,
        "uploaded_by": f.uploaded_by_id,
        "uploaded_by_name": f.uploaded_by.get_full_name() if f.uploaded_by else None,
        "created_at": _iso(f.created_at),
        "is_approved_teacher": f.is_approved_teacher,
        "approved_teacher_by": f.approved_teacher_by_id,
        "approved_teacher_at": _iso(f.approved_teacher_at),
        "is_approved_manager": f.is_approved_manager,
        "approved_manager_by": f.approved_manager_by_id,
        "approved_manager_at": _iso(f.approved_manager_at),
        "is_downloadable": f.is_downloadable,
    }


def material_to_dict(m: LibraryMaterial) -> dict:
    return {
        "id": m.id,
        "library": m.library_id,
        "library_name": m.library.name,
        "title": m.title,
        "topic": m.topic,
        "body": m.body,
        "status": m.status,
        "created_by": m.created_by_id,
        "created_by_name": m.created_by.get_full_name() if m.created_by else None,
        "published_at": _iso(m.published_at),
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
    }
