from django.contrib import admin

from .models import Exam, ExamResult, ExamType, Grade, Subject, Transcript


@admin.register(ExamType)
class ExamTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "color", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class ExamResultInline(admin.TabularInline):
    """Each exam's per-student scores, right under the exam."""

    model = ExamResult
    extra = 0
    fields = ("student", "score", "note", "graded_by", "graded_at")
    readonly_fields = ("graded_at",)
    raw_id_fields = ("student", "graded_by")
    show_change_link = True


@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "department", "is_active")
    list_filter = ("is_active", "department")
    search_fields = ("name", "code")


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ("title", "subject", "cohort", "term", "exam_type", "is_published", "exam_date")
    list_filter = ("exam_type", "is_published", "term")
    search_fields = ("title",)
    autocomplete_fields = ("subject", "cohort", "term", "exam_type", "created_by")
    date_hierarchy = "exam_date"
    inlines = (ExamResultInline,)


@admin.register(ExamResult)
class ExamResultAdmin(admin.ModelAdmin):
    list_display = ("exam", "student", "score", "graded_by", "graded_at")
    search_fields = ("student__student_id", "exam__title")
    autocomplete_fields = ("exam", "student", "graded_by")


@admin.register(Grade)
class GradeAdmin(admin.ModelAdmin):
    list_display = ("student", "subject", "term", "value_raw", "value_display", "is_published")
    list_filter = ("is_published", "term", "subject")
    search_fields = ("student__student_id",)
    autocomplete_fields = ("student", "subject", "term")


@admin.register(Transcript)
class TranscriptAdmin(admin.ModelAdmin):
    list_display = ("id", "student", "term", "status", "generated_at", "created_at")
    list_filter = ("status",)
    search_fields = ("student__student_id",)
    autocomplete_fields = ("student", "term", "requested_by")
