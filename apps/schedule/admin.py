from django.contrib import admin

from .models import Lesson, LessonType, RecurrenceRule, Term, TimeSlot


@admin.register(Term)
class TermAdmin(admin.ModelAdmin):
    list_display = ("academic_year", "name", "start_date", "end_date", "is_current")
    list_filter = ("academic_year", "is_current")
    search_fields = ("name", "academic_year")


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("branch", "name", "start_time", "end_time", "order")
    list_filter = ("branch",)
    search_fields = ("name",)
    autocomplete_fields = ("branch",)
    list_select_related = ("branch",)


@admin.register(LessonType)
class LessonTypeAdmin(admin.ModelAdmin):
    """Dynamic, per-Center lesson kind (F3-1) — mirrors academics.ExamType /
    students.EnrollmentReason so any curriculum can define its own kinds."""

    list_display = ("name", "slug", "color", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


class LessonInline(admin.TabularInline):
    """The concrete lessons expanded from this rule — materialized by the
    expansion service, so view-only here (edit a single occurrence from its own
    change page)."""

    model = Lesson
    extra = 0
    fields = ("title", "starts_at", "ends_at", "status", "teacher", "room", "detached_from_rule")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(RecurrenceRule)
class RecurrenceRuleAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "cohort",
        "teacher",
        "term",
        "lesson_type",
        "start_date",
        "end_date",
        "is_active",
    )
    list_filter = ("is_active", "term")
    search_fields = ("title",)
    autocomplete_fields = ("term", "cohort", "teacher", "room", "lesson_type", "created_by")
    list_select_related = ("term", "cohort", "teacher", "lesson_type")
    inlines = (LessonInline,)


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ("title", "cohort", "teacher", "room", "lesson_type", "starts_at", "ends_at", "status")
    list_filter = ("status", "term")
    search_fields = ("title",)
    autocomplete_fields = ("rule", "term", "cohort", "teacher", "room", "lesson_type")
    list_select_related = ("cohort", "teacher", "room", "lesson_type")
    date_hierarchy = "starts_at"
