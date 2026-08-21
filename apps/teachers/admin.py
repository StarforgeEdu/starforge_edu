from typing import cast

from django import forms
from django.contrib import admin

from apps.access.models import AccountType
from core.admin_mixins import RoleAccountAdminForm, RoleAccountAdminMixin
from core.permissions import Role

from .models import TeacherProfile, TeacherType


class TeacherProfileAdminForm(RoleAccountAdminForm):
    account_type = forms.ModelChoiceField(
        label="Account type",
        queryset=AccountType.objects.none(),
        required=True,
        help_text="Choose the permission set for this teacher account.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        queryset = AccountType.objects.filter(
            account_kind=AccountType.AccountKind.TEACHER,
            is_active=True,
        ).order_by("name")
        account_type_field = cast(forms.ModelChoiceField, self.fields["account_type"])
        account_type_field.queryset = queryset
        if self.instance.pk and self.instance.user_id:
            membership = (
                self.instance.user.role_memberships.filter(
                    revoked_at__isnull=True,
                    account_type__account_kind=AccountType.AccountKind.TEACHER,
                    account_type__is_active=True,
                )
                .select_related("account_type")
                .order_by("-account_type__is_system", "id")
                .first()
            )
            if membership is not None:
                self.initial["account_type"] = membership.account_type
        else:
            self.initial["account_type"] = queryset.filter(
                is_system=True,
                slug=Role.TEACHER,
            ).first()


@admin.register(TeacherType)
class TeacherTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "is_system", "is_default", "sort_order")
    list_filter = ("is_active", "is_system", "is_default")
    search_fields = ("name", "slug", "description")
    ordering = ("sort_order", "name")
    readonly_fields = ("is_system",)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.is_system:
            readonly.extend(("name", "slug", "is_active"))
        return tuple(dict.fromkeys(readonly))

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(TeacherProfile)
class TeacherProfileAdmin(RoleAccountAdminMixin):
    form = TeacherProfileAdminForm
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

        ensure_role_membership(
            obj,
            account_type=form.cleaned_data["account_type"],
            branch=obj.branch,
            department=obj.department,
        )
