from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin

from .models import ContentLesson, ContentLibrary, Course, FileView, Folder, LessonFile, Module


class FolderInline(admin.TabularInline):
    """Folders that live directly in a library (hand-authored)."""

    model = Folder
    extra = 0
    fields = ("name", "parent")
    autocomplete_fields = ("parent",)
    show_change_link = True


class ModuleInline(admin.TabularInline):
    """A course's modules, in order (hand-authored)."""

    model = Module
    extra = 0
    fields = ("title", "order")
    show_change_link = True


class ContentLessonInline(admin.TabularInline):
    """A module's lessons, in order (hand-authored)."""

    model = ContentLesson
    extra = 0
    fields = ("title", "order")
    show_change_link = True


@admin.register(ContentLibrary)
class ContentLibraryAdmin(admin.ModelAdmin):
    list_display = ("name", "visibility", "department", "cohort", "is_active")
    list_filter = ("visibility", "is_active")
    search_fields = ("name",)
    autocomplete_fields = ("department", "cohort")
    list_select_related = ("department", "cohort")
    inlines = (FolderInline,)


@admin.register(Course)
class CourseAdmin(admin.ModelAdmin):
    list_display = ("title", "library", "subject", "order")
    search_fields = ("title",)
    autocomplete_fields = ("library", "subject")
    list_select_related = ("library", "subject")
    inlines = (ModuleInline,)


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("title", "course", "order")
    search_fields = ("title",)
    autocomplete_fields = ("course",)
    list_select_related = ("course",)
    inlines = (ContentLessonInline,)


@admin.register(ContentLesson)
class ContentLessonAdmin(admin.ModelAdmin):
    list_display = ("title", "module", "order")
    search_fields = ("title",)
    autocomplete_fields = ("module",)
    list_select_related = ("module",)


@admin.register(Folder)
class FolderAdmin(admin.ModelAdmin):
    list_display = ("name", "library", "parent")
    search_fields = ("name",)
    autocomplete_fields = ("library", "parent")
    list_select_related = ("library", "parent")


@admin.register(LessonFile)
class LessonFileAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "content_type", "size_bytes", "version", "download_count")
    list_filter = ("status",)
    search_fields = ("title", "s3_key")
    autocomplete_fields = (
        "lesson",
        "folder",
        "previous_version",
        "uploaded_by",
        "approved_teacher_by",
        "approved_manager_by",
    )


@admin.register(FileView)
class FileViewAdmin(ReadOnlyAdmin):
    """The file view/download access log — written by the streaming service, so
    view-only here (matches the audit/ledger pattern)."""

    list_display = ("file", "user", "action", "created_at")
    list_filter = ("action",)
    search_fields = ("file__title", "user__username")
    autocomplete_fields = ("file", "user")
    list_select_related = ("file", "user")
    date_hierarchy = "created_at"
