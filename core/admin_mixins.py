"""Shared admin mixins.

``ReadOnlyAdmin`` makes a model view-only in ``/admin/``: still searchable and
filterable for review, but no add / change / delete. It's for append-only or
system-managed tables where hand-editing would corrupt an invariant — the money
ledger and approval state machine (anti-fraud "money can't disappear" trail), the
auth sessions (the ``key`` is a live Bearer token), and the campaign send-log
(rows are frozen at build time, not authored by hand).

Registering a model with any explicit admin (including this one) also makes the
``core.admin_autoregister`` sweep skip it — the sweep only touches models that
nothing has registered yet.
"""

from __future__ import annotations

from typing import ClassVar

from django import forms
from django.contrib import admin
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpRequest


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: object | None = None) -> bool:
        return False


class RoleAccountAdminForm(forms.ModelForm):
    """Admin form for role-owned accounts; the internal User bridge never appears."""

    password1 = forms.CharField(
        label="New password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text="Optional. Leave blank to keep the current password (or create a disabled login).",
    )
    password2 = forms.CharField(
        label="Confirm new password",
        required=False,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    class Meta:
        exclude = ("user", "password")  # noqa: DJ006 - generic form spans four role models

    def clean_username(self) -> str:
        from apps.users.services import validate_role_username
        from core.exceptions import ValidationException

        value = self.cleaned_data.get("username") or ""
        try:
            return validate_role_username(
                value,
                bridge_user_id=getattr(self.instance, "user_id", None),
                account=self.instance,
            )
        except ValidationException as exc:
            raise forms.ValidationError(str(exc.detail)) from exc

    def clean(self):
        cleaned = super().clean() or {}
        first = cleaned.get("password1") or ""
        second = cleaned.get("password2") or ""
        if first != second:
            self.add_error("password2", "The two password fields do not match.")
        elif first:
            try:
                validate_password(first, user=self.instance)
            except DjangoValidationError as exc:
                self.add_error("password1", exc)
        return cleaned

    def save(self, commit: bool = True):
        account = super().save(commit=False)
        raw_password = self.cleaned_data.get("password1") or ""
        if raw_password:
            account.set_password(raw_password)
        elif account._state.adding and not account.password:
            account.set_unusable_password()
        if commit:
            account.save()
            self.save_m2m()
        return account


class RoleAccountAdminMixin(admin.ModelAdmin):
    """Make a role model the only operator-facing identity/login surface."""

    form = RoleAccountAdminForm
    exclude: ClassVar[tuple[str, ...]] = ("user", "password")
    readonly_fields: ClassVar[tuple[str, ...]] = ("last_login_at",)

    def save_model(self, request, obj, form, change) -> None:
        from apps.users.services import sync_role_user_bridge

        sync_role_user_bridge(obj)
        super().save_model(request, obj, form, change)
        if form.cleaned_data.get("password1"):
            from core.session_auth import revoke_all_for_user

            revoke_all_for_user(obj.user_id)
