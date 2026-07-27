from django.contrib import admin

from core.admin_mixins import RoleAccountAdminMixin

from .models import TeacherProfile


@admin.register(TeacherProfile)
class TeacherProfileAdmin(RoleAccountAdminMixin):
    list_display = (
        "username",
        "first_name",
        "last_name",
        "phone",
        "branch",
        "department",
        "salary_type",
        "is_substitute",
    )
    list_filter = ("salary_type", "is_substitute", "branch", "gender")
    search_fields = ("username", "first_name", "last_name", "phone", "email")
    autocomplete_fields = ("branch", "department")
    list_select_related = ("branch", "department")

    def save_model(self, request, obj, form, change) -> None:
        super().save_model(request, obj, form, change)
        from apps.users.services import ensure_role_membership
        from core.permissions import Role

        ensure_role_membership(
            obj,
            role=Role.TEACHER,
            branch=obj.branch,
            department=obj.department,
        )
