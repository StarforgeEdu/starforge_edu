from typing import ClassVar

from django import forms
from django.contrib import admin

from apps.teachers.models import TeacherProfile
from core.admin_mixins import RoleAccountAdminForm, RoleAccountAdminMixin

from .models import (
    Branch,
    BranchHoliday,
    BranchTransfer,
    BranchWorkingHours,
    CenterSettings,
    Department,
    Room,
    StaffProfile,
)


class StaffProfileAdminForm(RoleAccountAdminForm):
    role = forms.ChoiceField(choices=(), required=True)
    branch = forms.ModelChoiceField(queryset=Branch.objects.all(), required=True)
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.org.services import STAFF_ROLES

        role_field = self.fields["role"]
        if isinstance(role_field, forms.ChoiceField):
            role_field.choices = [(role, role.replace("_", " ").title()) for role in STAFF_ROLES]
        if self.instance.pk and self.instance.user_id:
            membership = (
                self.instance.user.role_memberships.filter(revoked_at__isnull=True).order_by("id").first()
            )
            if membership is not None:
                self.initial.update(
                    role=membership.role,
                    branch=membership.branch,
                    department=membership.department,
                )

    def clean(self):
        cleaned = super().clean() or {}
        branch = cleaned.get("branch")
        department = cleaned.get("department")
        if branch is not None and department is not None and department.branch_id != branch.pk:
            self.add_error("department", "Department must belong to the selected branch.")
        return cleaned


@admin.register(StaffProfile)
class StaffProfileAdmin(RoleAccountAdminMixin):
    form = StaffProfileAdminForm
    list_display = ("username", "first_name", "last_name", "phone", "email", "is_active", "created_at")
    list_filter = ("is_active", "gender")
    search_fields = ("first_name", "last_name", "phone", "email", "username")
    readonly_fields: ClassVar[tuple[str, ...]] = (
        "last_login_at",
        "created_at",
        "updated_at",
    )

    def save_model(self, request, obj, form, change) -> None:
        super().save_model(request, obj, form, change)
        from apps.users.services import ensure_role_membership

        ensure_role_membership(
            obj,
            role=form.cleaned_data["role"],
            branch=form.cleaned_data["branch"],
            department=form.cleaned_data.get("department"),
        )


class BranchWorkingHoursInline(admin.TabularInline):
    model = BranchWorkingHours
    extra = 0
    max_num = 7


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "phone", "is_active", "archived_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "address")
    inlines = (BranchWorkingHoursInline,)


class DepartmentAdminForm(forms.ModelForm):
    teacher_head = forms.ModelChoiceField(
        label="Head teacher",
        queryset=TeacherProfile.objects.select_related("branch"),
        required=False,
    )

    class Meta:
        model = Department
        fields = (
            "branch",
            "name",
            "slug",
            "description",
            "is_active",
            "teacher_head",
            "budget",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.head_id:
            self.initial["teacher_head"] = getattr(self.instance.head, "teacher_profile", None)

    def clean(self):
        cleaned = super().clean() or {}
        branch = cleaned.get("branch")
        teacher = cleaned.get("teacher_head")
        if branch is not None and teacher is not None and teacher.branch_id != branch.pk:
            self.add_error("teacher_head", "Head teacher must belong to this branch.")
        return cleaned

    def save(self, commit=True):
        department = super().save(commit=False)
        teacher = self.cleaned_data.get("teacher_head")
        department.head = teacher.user if teacher is not None else None
        if commit:
            department.save()
            self.save_m2m()
        return department


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    form = DepartmentAdminForm
    list_display = ("name", "branch", "slug", "head_teacher", "budget", "is_active")
    list_filter = ("is_active", "branch")
    search_fields = ("name", "slug")

    @admin.display(description="Head teacher", ordering="head__teacher_profile__last_name")
    def head_teacher(self, obj: Department) -> str:
        teacher = getattr(obj.head, "teacher_profile", None) if obj.head else None
        return str(teacher) if teacher is not None else "-"


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "capacity", "is_active")
    list_filter = ("is_active", "branch")
    search_fields = ("name",)


@admin.register(BranchHoliday)
class BranchHolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "branch", "is_working_day_override")
    list_filter = ("branch", "is_working_day_override")
    search_fields = ("name",)
    date_hierarchy = "date"


@admin.register(BranchTransfer)
class BranchTransferAdmin(admin.ModelAdmin):
    """Read-only audit surface — transfers are written by services only."""

    list_display = ("user", "from_branch", "to_branch", "reason", "actor", "created_at")
    list_filter = ("from_branch", "to_branch")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CenterSettings)
class CenterSettingsAdmin(admin.ModelAdmin):
    """The per-tenant singleton (pk=1) — operator repair surface (TD-10)."""

    list_display = ("__str__", "grading_scheme", "currency_primary", "student_id_pattern", "updated_at")

    def has_add_permission(self, request):
        # Singleton: created lazily by CenterSettings.load(), never via admin.
        return not CenterSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
