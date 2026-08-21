from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin

from .models import Assignment, AssignmentUploadGrant, Submission, SubmissionGrade


class SubmissionInline(admin.TabularInline):
    """The turn-ins under an assignment — view-only context (students submit via the
    API); the late flag / status are edited on the submission's own page."""

    model = Submission
    extra = 0
    fields = ("student", "attempt_number", "is_late", "status", "submitted_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:
        return False


class SubmissionGradeInline(admin.StackedInline):
    model = SubmissionGrade
    extra = 0
    autocomplete_fields = ("graded_by",)


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "cohort",
        "status",
        "due_at",
        "max_resubmits",
        "attachment_count",
        "published_at",
    )
    list_filter = ("status",)
    search_fields = ("title",)
    autocomplete_fields = ("cohort", "created_by")
    list_select_related = ("cohort",)
    date_hierarchy = "due_at"
    exclude = ("attachments",)
    inlines = (SubmissionInline,)

    @admin.display(description="Attachments")
    def attachment_count(self, obj: Assignment) -> int:
        return len(obj.attachments) if isinstance(obj.attachments, list) else 0


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    # `is_late` is the "mark late" checkbox; `attempt_number` (>1) is a resubmit /
    # second chance — both editable here (owner: "where's the checkmark to mark it late").
    list_display = (
        "assignment",
        "student",
        "attempt_number",
        "is_late",
        "status",
        "attachment_count",
        "submitted_at",
    )
    list_filter = ("status", "is_late")
    search_fields = ("student__student_id", "assignment__title")
    autocomplete_fields = ("assignment", "student")
    list_select_related = ("assignment", "student__user")
    exclude = ("attachments",)
    inlines = (SubmissionGradeInline,)

    @admin.display(description="Attachments")
    def attachment_count(self, obj: Submission) -> int:
        return len(obj.attachments) if isinstance(obj.attachments, list) else 0


@admin.register(SubmissionGrade)
class SubmissionGradeAdmin(admin.ModelAdmin):
    list_display = ("submission", "score", "graded_by", "graded_at")
    search_fields = ("submission__student__student_id",)
    autocomplete_fields = ("submission", "graded_by")


@admin.register(AssignmentUploadGrant)
class AssignmentUploadGrantAdmin(ReadOnlyAdmin):
    """Storage capabilities are system-owned and raw object keys stay hidden."""

    list_display = (
        "id",
        "requested_by",
        "content_type",
        "expected_size_bytes",
        "consumed_at",
        "source_deleted_at",
        "expires_at",
    )
    list_filter = ("content_type", "consumed_at", "source_deleted_at")
    search_fields = ("requested_by__username",)
    list_select_related = ("requested_by",)
    exclude = ("key", "durable_key")
