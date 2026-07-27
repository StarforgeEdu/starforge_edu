from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Q

from apps.org.models import StaffProfile
from apps.parents.models import ParentProfile
from apps.students.models import StudentProfile
from apps.teachers.models import TeacherProfile
from core.admin_mixins import ReadOnlyAdmin

from .models import OTP, Device, RoleMembership, Session, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("-date_joined",)
    list_display = ("username", "phone", "email", "first_name", "last_name", "is_staff", "is_active")
    search_fields = ("username", "phone", "email", "first_name", "last_name")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Contacts", {"fields": ("phone", "email")}),
        ("Identity", {"fields": ("first_name", "middle_name", "last_name")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Activity", {"fields": ("last_login", "last_seen_at", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "phone", "email", "password1", "password2"),
            },
        ),
    )
    readonly_fields = ("last_login", "last_seen_at", "date_joined")

    def get_queryset(self, request):
        # Role accounts live in their own admin sections. Hide their automatically
        # provisioned compatibility principals from the platform User table.
        return super().get_queryset(request).filter(Q(is_staff=True) | Q(is_superuser=True))

    def save_model(self, request, obj, form, change) -> None:
        # This table is exclusively for Django-admin operators. Role accounts are
        # created from their own admin sections and never appear here.
        if not change:
            obj.is_staff = True
        super().save_model(request, obj, form, change)


@admin.register(OTP)
class OTPAdmin(admin.ModelAdmin):
    list_display = ("identifier", "channel", "purpose", "consumed_at", "expires_at", "attempts", "created_at")
    list_filter = ("channel", "purpose")
    search_fields = ("identifier",)


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "platform", "device_id", "last_seen_at", "revoked_at")
    list_filter = ("platform",)
    search_fields = ("user__username", "user__phone", "user__email", "device_id")


class RoleMembershipAdminForm(forms.ModelForm):
    """Assign roles to a named role account without exposing the User bridge."""

    _selected_account: Any = None

    staff_account = forms.ModelChoiceField(
        label="Staff account",
        queryset=StaffProfile.objects.all(),
        required=False,
        help_text="Choose exactly one account across these four fields.",
    )
    teacher_account = forms.ModelChoiceField(
        label="Teacher account",
        queryset=TeacherProfile.objects.all(),
        required=False,
        help_text="Choose exactly one account across these four fields.",
    )
    student_account = forms.ModelChoiceField(
        label="Student account",
        queryset=StudentProfile.objects.all(),
        required=False,
        help_text="Choose exactly one account across these four fields.",
    )
    parent_account = forms.ModelChoiceField(
        label="Parent account",
        queryset=ParentProfile.objects.all(),
        required=False,
        help_text="Choose exactly one account across these four fields.",
    )

    class Meta:
        model = RoleMembership
        fields = ("role", "branch", "department", "revoked_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.user_id:
            user = self.instance.user
            for field, relation in (
                ("staff_account", "staff_profile"),
                ("teacher_account", "teacher_profile"),
                ("student_account", "student_profile"),
                ("parent_account", "parent_profile"),
            ):
                account = getattr(user, relation, None)
                if account is not None:
                    self.initial[field] = account
                    break

    def clean(self):
        cleaned = super().clean() or {}
        selected = [
            cleaned.get(name)
            for name in ("staff_account", "teacher_account", "student_account", "parent_account")
            if cleaned.get(name) is not None
        ]
        if len(selected) != 1:
            raise forms.ValidationError("Choose exactly one staff, teacher, student, or parent account.")
        self._selected_account = selected[0]
        return cleaned

    def save(self, commit=True):
        membership = super().save(commit=False)
        if self._selected_account is None:  # defensive; clean() enforces this
            raise ValueError("A role account must be selected.")
        membership.user = self._selected_account.user
        if commit:
            membership.save()
            self.save_m2m()
        return membership


@admin.register(RoleMembership)
class RoleMembershipAdmin(admin.ModelAdmin):
    form = RoleMembershipAdminForm
    fields = (
        "staff_account",
        "teacher_account",
        "student_account",
        "parent_account",
        "role",
        "branch",
        "department",
        "revoked_at",
    )
    list_display = ("role_account", "role", "branch", "department", "granted_at", "revoked_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__phone", "user__email")

    @admin.display(description="Account", ordering="user__username")
    def role_account(self, obj: RoleMembership) -> str:
        for relation in ("staff_profile", "teacher_profile", "student_profile", "parent_profile"):
            account = getattr(obj.user, relation, None)
            if account is not None:
                return str(account)
        return "Legacy account"

    def save_model(self, request, obj, form, change) -> None:
        if not change and obj.granted_by_id is None:
            obj.granted_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Session)
class SessionAdmin(ReadOnlyAdmin):
    """View-only. ``key`` is a live Bearer token — never list, search, or render
    it (exposing it lets a viewer impersonate the user), so it is excluded from
    the form entirely. Session lifecycle is owned by the auth service."""

    list_display = (
        "id",
        "user",
        "ip_address",
        "device_id",
        "read_only",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
    )
    list_filter = ("read_only",)
    search_fields = ("user__username", "ip_address", "device_id")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    exclude = ("key",)
