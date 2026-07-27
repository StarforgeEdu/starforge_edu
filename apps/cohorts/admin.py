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
    """Co-teachers / assistants assigned to this cohort."""

    model = CohortTeacher
    extra = 0
    fields = ("teacher", "role")
    autocomplete_fields = ("teacher",)
    show_change_link = True


@admin.register(Cohort)
class CohortAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "department", "is_archived", "start_date", "end_date")
    list_filter = ("is_archived", "branch")
    search_fields = ("name", "level")
    autocomplete_fields = ("branch", "department", "primary_teacher", "default_room")
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
    list_display = ("cohort", "teacher", "role")
    list_filter = ("role",)
    search_fields = ("cohort__name", "teacher__user__first_name", "teacher__user__last_name")
    autocomplete_fields = ("cohort", "teacher")
    list_select_related = ("cohort", "teacher")
