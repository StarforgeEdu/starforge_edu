from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin, RoleAccountAdminMixin

from .models import EnrollmentEvent, EnrollmentReason, StudentIdCounter, StudentProfile


@admin.register(EnrollmentReason)
class EnrollmentReasonAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "color", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


class EnrollmentEventInline(admin.TabularInline):
    """The student's status-change history, read-only (written by the transition
    service, not by hand)."""

    model = EnrollmentEvent
    extra = 0
    fields = ("from_status", "to_status", "reason_code", "note", "actor", "created_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(StudentProfile)
class StudentProfileAdmin(RoleAccountAdminMixin):
    exclude = (
        *RoleAccountAdminMixin.exclude,
        "medical_notes",
        "emergency_contacts",
    )
    list_display = (
        "student_id",
        "username",
        "first_name",
        "last_name",
        "phone",
        "status",
        "branch",
        "enrollment_date",
    )
    list_filter = ("status", "branch", "gender")
    search_fields = ("student_id", "username", "first_name", "last_name", "phone", "email")
    list_select_related = ("branch", "current_cohort")
    readonly_fields = (
        *RoleAccountAdminMixin.readonly_fields,
        "student_id",
        "status",
        "branch",
        "current_cohort",
        "enrollment_date",
        "blocked_at",
        "block_reason",
        "created_at",
        "updated_at",
    )
    inlines = (EnrollmentEventInline,)

    def has_add_permission(self, request) -> bool:
        # Creation owns generated IDs, tenant seat limits, initial enrollment
        # events, and branch-scoped grants; the domain service is the only safe
        # path that performs those writes atomically.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).defer("medical_notes", "emergency_contacts")

    def save_model(self, request, obj, form, change) -> None:
        if change:
            from apps.users.models import User
            from core.identity_lifecycle import assert_exclusive_role_bridge

            User.objects.select_for_update().get(pk=obj.user_id)
            assert_exclusive_role_bridge(obj, principal_kind="student")
        super().save_model(request, obj, form, change)
        from apps.users.services import ensure_role_membership
        from core.permissions import Role

        if obj.is_active and obj.user.is_active:
            ensure_role_membership(obj, role=Role.STUDENT, branch=obj.branch)


@admin.register(EnrollmentEvent)
class EnrollmentEventAdmin(ReadOnlyAdmin):
    """The enrollment status-change log — written by the transition service, so
    view-only here (matches the audit/ledger pattern)."""

    list_display = ("student", "from_status", "to_status", "reason_code", "actor", "created_at")
    list_filter = ("to_status", "reason_code")
    search_fields = ("student__student_id", "reason_code")
    autocomplete_fields = ("student", "actor")
    date_hierarchy = "created_at"


@admin.register(StudentIdCounter)
class StudentIdCounterAdmin(ReadOnlyAdmin):
    """Internal per-year sequence that mints student IDs — never hand-edited."""

    list_display = ("year", "last_value")
