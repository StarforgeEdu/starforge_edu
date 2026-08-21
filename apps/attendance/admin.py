from django.contrib import admin

from .models import AttendanceRecord


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = (
        "student",
        "group",
        "teacher",
        "lesson",
        "status",
        "auto_marked",
        "marked_by",
        "marked_at",
    )
    list_filter = ("status", "auto_marked")
    search_fields = ("student__student_id", "lesson__title")
    autocomplete_fields = ("student", "marked_by")
    raw_id_fields = ("lesson",)  # schedule.LessonAdmin has no search_fields -> can't autocomplete
    list_select_related = ("student__user", "lesson__cohort", "lesson__teacher__user", "marked_by")
    date_hierarchy = "created_at"

    @admin.display(description="Group")
    def group(self, obj: AttendanceRecord) -> str:
        return obj.lesson.cohort.name

    @admin.display(description="Teacher")
    def teacher(self, obj: AttendanceRecord) -> str:
        return obj.lesson.teacher.get_full_name()
