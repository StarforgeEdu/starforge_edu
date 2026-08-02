from django.contrib import admin

from core.admin_mixins import ReadOnlyAdmin, RoleAccountAdminMixin

from .models import Guardian, ParentProfile, PickupAuthorization


class GuardianInline(admin.TabularInline):
    """Historical family links; corrections use the audited API workflow."""

    model = Guardian
    extra = 0
    fields = ("student", "relationship", "is_primary", "revoked_at", "revoked_by")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .defer(
                "custody_notes",
                "student__medical_notes",
                "student__emergency_contacts",
            )
        )

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ParentProfile)
class ParentProfileAdmin(RoleAccountAdminMixin):
    exclude = (*RoleAccountAdminMixin.exclude, "notes")
    list_display = (
        "username",
        "first_name",
        "last_name",
        "phone",
        "workplace",
        "attribution_status",
        "branch_at_creation",
        "department_at_creation",
        "created_at",
    )
    list_filter = (
        "gender",
        "attribution_status",
        "branch_at_creation",
        "department_at_creation",
    )
    list_select_related = ("branch_at_creation", "department_at_creation")
    search_fields = ("username", "first_name", "last_name", "phone", "email")
    readonly_fields = (
        *RoleAccountAdminMixin.readonly_fields,
        "attribution_status",
        "branch_at_creation",
        "department_at_creation",
        "created_by",
    )
    inlines = (GuardianInline,)

    def has_add_permission(self, request) -> bool:
        # Parent creation must capture an immutable permission-bearing branch /
        # department boundary. The generic admin form has no safe way to derive
        # that attribution from an arbitrary administrator session.
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).defer("notes")

    def save_model(self, request, obj, form, change) -> None:
        if change:
            from apps.users.models import User
            from core.identity_lifecycle import assert_exclusive_role_bridge

            User.objects.select_for_update().get(pk=obj.user_id)
            assert_exclusive_role_bridge(obj, principal_kind="parent")
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change) -> None:
        super().save_related(request, form, formsets, change)
        from apps.users.services import ensure_role_membership
        from core.permissions import Role

        parent = form.instance
        if not parent.is_active or not parent.user.is_active:
            return
        for branch in {
            guardian.student.branch
            for guardian in parent.guardianships.filter(revoked_at__isnull=True)
            .select_related("student__branch")
            .defer(
                "custody_notes",
                "student__medical_notes",
                "student__emergency_contacts",
            )
            .all()
        }:
            ensure_role_membership(
                parent,
                role=Role.PARENT,
                branch=branch,
                department=None,
                replace_scope=False,
            )


@admin.register(Guardian)
class GuardianAdmin(ReadOnlyAdmin):
    exclude = ("custody_notes",)
    list_display = ("parent", "student", "relationship", "is_primary")
    list_filter = ("relationship", "is_primary")
    list_select_related = ("parent", "student")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .defer(
                "custody_notes",
                "parent__notes",
                "student__medical_notes",
                "student__emergency_contacts",
            )
        )


@admin.register(PickupAuthorization)
class PickupAuthorizationAdmin(ReadOnlyAdmin):
    list_display = ("student", "full_name", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("full_name", "phone")
    list_select_related = ("student",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .defer(
                "student__medical_notes",
                "student__emergency_contacts",
            )
        )
