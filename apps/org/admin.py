from typing import ClassVar, cast

from django import forms
from django.contrib import admin

from apps.access.models import AccountType
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
    account_type = forms.ModelChoiceField(
        label="Account type",
        queryset=AccountType.objects.none(),
        required=True,
        help_text="Choose the permission set for this staff account.",
    )
    branch = forms.ModelChoiceField(queryset=Branch.objects.all(), required=True)
    department = forms.ModelChoiceField(queryset=Department.objects.all(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        account_type_field = cast(forms.ModelChoiceField, self.fields["account_type"])
        account_type_field.queryset = AccountType.objects.filter(
            account_kind=AccountType.AccountKind.STAFF,
            is_active=True,
        ).order_by("name")
        if self.instance.pk and self.instance.user_id:
            membership = (
                self.instance.user.role_memberships.filter(
                    revoked_at__isnull=True,
                    account_type__account_kind=AccountType.AccountKind.STAFF,
                    account_type__is_active=True,
                )
                .select_related("account_type", "branch", "department")
                .order_by("-account_type__is_system", "id")
                .first()
            )
            if membership is not None:
                self.initial.update(
                    account_type=membership.account_type,
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
            account_type=form.cleaned_data["account_type"],
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

    def has_delete_permission(self, request, obj=None):
        return False


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

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "capacity", "is_active")
    list_filter = ("is_active", "branch")
    search_fields = ("name",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BranchHoliday)
class BranchHolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "branch", "is_working_day_override")
    list_filter = ("branch", "is_working_day_override")
    search_fields = ("name",)
    date_hierarchy = "date"


@admin.register(BranchTransfer)
class BranchTransferAdmin(admin.ModelAdmin):
    """Read-only audit surface — transfers are written by services only."""

    list_display = (
        "student_public_id",
        "student_name",
        "from_branch",
        "to_branch",
        "reason",
        "actor_name",
        "created_at",
    )
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

    list_display = (
        "__str__",
        "organization_timezone",
        "grading_scheme",
        "currency_primary",
        "student_id_pattern",
        "updated_at",
    )

    def has_add_permission(self, request):
        # Provisioning/migrations own the singleton; admin may repair it only in place.
        return False

    def has_delete_permission(self, request, obj=None):
        return False
