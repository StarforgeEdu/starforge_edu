from django.contrib import admin

from apps.reports.models import Report, ReportRun, ReportSchedule
from core.admin_mixins import ReadOnlyAdmin


class ReportRunInline(admin.TabularInline):
    """The report's generation history, read-only (rows are written by the
    off-request build task, never authored by hand)."""

    model = ReportRun
    extra = 0
    fields = ("status", "format", "file_bytes", "requested_by", "created_at", "finished_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:
        return False


class ReportScheduleInline(admin.TabularInline):
    """The report's recurring schedules."""

    model = ReportSchedule
    extra = 0
    fields = ("cadence", "weekday", "day_of_month", "hour", "format", "is_active", "last_run_at", "created_by")
    readonly_fields = ("last_run_at",)
    autocomplete_fields = ("created_by",)
    show_change_link = True


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("key", "title", "default_format")
    list_filter = ("default_format",)
    search_fields = ("key", "title")
    inlines = (ReportRunInline, ReportScheduleInline)


@admin.register(ReportRun)
class ReportRunAdmin(ReadOnlyAdmin):
    """The report generation log — written by the build task, so view-only here
    (matches the audit/ledger pattern)."""

    list_display = ("id", "report", "format", "status", "file_bytes", "created_at", "finished_at")
    list_filter = ("status", "format", "report")
    search_fields = ("s3_key",)
    autocomplete_fields = ("report", "requested_by")
    list_select_related = ("report", "requested_by")
    readonly_fields = ("created_at", "started_at", "finished_at")
    date_hierarchy = "created_at"


@admin.register(ReportSchedule)
class ReportScheduleAdmin(admin.ModelAdmin):
    list_display = ("id", "report", "cadence", "weekday", "day_of_month", "hour", "is_active", "last_run_at")
    list_filter = ("cadence", "is_active", "report")
    autocomplete_fields = ("report", "created_by")
    list_select_related = ("report", "created_by")
    readonly_fields = ("last_run_at", "created_at", "updated_at")
