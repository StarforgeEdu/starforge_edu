"""Django-admin configuration for canonical tenant account types."""

from django import forms
from django.contrib import admin

from apps.access.models import AccountType, AccountTypePermission, RolePermissionOverride
from apps.access.validation import permission_catalogue, permission_catalogue_metadata


def _permission_choices() -> list[tuple[str, str]]:
    metadata = {item["code"]: item["label"] for item in permission_catalogue_metadata()}
    codes = permission_catalogue()
    codes.update(f"{code.partition(':')[0]}:*" for code in tuple(codes))
    return [(code, metadata.get(code, code.replace("_", " ").title())) for code in sorted(codes)]


class AccountTypePermissionAdminForm(forms.ModelForm):
    permission = forms.ChoiceField(
        choices=(),
        help_text="Choose one server-enforced capability for this account type.",
    )

    class Meta:
        model = AccountTypePermission
        fields = ("account_type", "permission")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        permission_field = self.fields["permission"]
        if isinstance(permission_field, forms.ChoiceField):
            permission_field.choices = _permission_choices()


class AccountTypePermissionInline(admin.TabularInline):
    model = AccountTypePermission
    form = AccountTypePermissionAdminForm
    extra = 0
    fields = ("permission", "created_at")
    readonly_fields = ("created_at",)

    def has_add_permission(self, request, obj=None) -> bool:
        return bool(obj is None or not obj.is_owner_type)

    def has_change_permission(self, request, obj=None) -> bool:
        return bool(obj is None or not obj.is_owner_type)

    def has_delete_permission(self, request, obj=None) -> bool:
        return bool(obj is None or not obj.is_owner_type)


@admin.register(AccountType)
class AccountTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "account_kind", "is_active", "is_system", "updated_at")
    list_filter = ("account_kind", "is_active", "is_system")
    search_fields = ("name", "slug", "description")
    ordering = ("account_kind", "name")
    inlines = (AccountTypePermissionInline,)

    def get_readonly_fields(self, request, obj=None):
        fields = ["is_system", "created_at", "updated_at"]
        if obj is not None and obj.is_owner_type:
            fields.extend(("name", "slug", "account_kind", "description", "is_active"))
        return tuple(fields)

    def has_delete_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(AccountTypePermission)
class AccountTypePermissionAdmin(admin.ModelAdmin):
    form = AccountTypePermissionAdminForm
    list_display = ("account_type", "permission", "created_at")
    list_filter = ("account_type__account_kind", "account_type__is_system")
    search_fields = ("account_type__name", "account_type__slug", "permission")
    readonly_fields = ("created_at",)

    def has_add_permission(self, request) -> bool:
        # Grants are edited through the AccountType inline, where the protected
        # owner type is known and can be made immutable before form submission.
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.account_type.is_owner_type:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.account_type.is_owner_type:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(RolePermissionOverride)
class RolePermissionOverrideAdmin(admin.ModelAdmin):
    """Read-only compatibility view; API writes synchronize canonical grants."""

    list_display = ("role", "permission", "effect", "updated_at")
    list_filter = ("role", "effect")
    search_fields = ("role", "permission", "note")
    readonly_fields = ("role", "permission", "effect", "note", "created_at", "updated_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
