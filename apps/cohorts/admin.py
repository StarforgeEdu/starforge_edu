from django.contrib import admin

from .models import Cohort, CohortMembership, CohortTeacher


class CohortMembershipInline(admin.TabularInline):
    """Students placed in this cohort (with move history)."""

    model = CohortMembership
    extra = 0
    fields = ("student", "start_date", "end_date", "moved_reason")
    autocomplete_fields = ("student",)
    show_change_link = True


class CohortTeacherInline(admin.TabularInline):
    """Typed teachers assigned to this cohort."""

    model = CohortTeacher
    extra = 0
    fields = ("teacher", "teacher_type")
    autocomplete_fields = ("teacher", "teacher_type")
    show_change_link = True


@admin.register(Cohort)
class CohortAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "branch",
        "department",
        "study_month",
        "lesson_cycle_length",
        "audience_type",
        "is_archived",
        "start_date",
        "end_date",
    )
    list_filter = (
        "is_archived",
        "branch",
        "audience_type",
        "study_month",
        "lesson_cycle_length",
    )
    search_fields = ("name", "level")
    # ``primary_teacher`` is a read-compatible projection of the canonical typed
    # assignments. Hiding it prevents two competing teacher editors in admin;
    # Main Teacher and every additional type are managed through the inline.
    exclude = ("primary_teacher",)
    autocomplete_fields = ("branch", "department", "default_room")
    list_select_related = ("branch", "department")
    inlines = (CohortMembershipInline, CohortTeacherInline)


@admin.register(CohortMembership)
class CohortMembershipAdmin(admin.ModelAdmin):
    list_display = ("cohort", "student", "start_date", "end_date")
    list_filter = ("start_date",)
    search_fields = ("cohort__name", "student__student_id")
    autocomplete_fields = ("cohort", "student")
    list_select_related = ("cohort", "student")


@admin.register(CohortTeacher)
class CohortTeacherAdmin(admin.ModelAdmin):
    list_display = ("cohort", "teacher", "teacher_type")
    list_filter = ("teacher_type",)
    search_fields = ("cohort__name", "teacher__user__first_name", "teacher__user__last_name")
    autocomplete_fields = ("cohort", "teacher", "teacher_type")
    exclude = ("role",)
    list_select_related = ("cohort", "teacher", "teacher_type")
