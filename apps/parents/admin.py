from django.contrib import admin

from core.admin_mixins import RoleAccountAdminMixin

from .models import Guardian, ParentProfile, PickupAuthorization


class GuardianInline(admin.TabularInline):
    """The parent's guardianship links to students (editable)."""

    model = Guardian
    extra = 0
    fields = ("student", "relationship", "is_primary", "custody_notes")
    autocomplete_fields = ("student",)
    show_change_link = True


@admin.register(ParentProfile)
class ParentProfileAdmin(RoleAccountAdminMixin):
    list_display = ("username", "first_name", "last_name", "phone", "workplace", "created_at")
    list_filter = ("gender",)
    search_fields = ("username", "first_name", "last_name", "phone", "email")
    inlines = (GuardianInline,)

    def save_related(self, request, form, formsets, change) -> None:
        super().save_related(request, form, formsets, change)
        from apps.users.models import RoleMembership
        from core.permissions import Role

        parent = form.instance
        for branch in {
            guardian.student.branch
            for guardian in parent.guardianships.select_related("student__branch").all()
        }:
            RoleMembership.objects.get_or_create(
                user=parent.user,
                role=Role.PARENT,
                branch=branch,
                department=None,
            )


@admin.register(Guardian)
class GuardianAdmin(admin.ModelAdmin):
    list_display = ("parent", "student", "relationship", "is_primary")
    list_filter = ("relationship", "is_primary")
    autocomplete_fields = ("parent", "student")
    list_select_related = ("parent", "student")


@admin.register(PickupAuthorization)
class PickupAuthorizationAdmin(admin.ModelAdmin):
    list_display = ("student", "full_name", "phone", "is_active")
    list_filter = ("is_active",)
    search_fields = ("full_name", "phone")
    autocomplete_fields = ("student",)
    list_select_related = ("student",)
