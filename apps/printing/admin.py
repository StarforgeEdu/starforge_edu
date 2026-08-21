"""Printing admin (D4-LD-1). Read-mostly operational views."""

from __future__ import annotations

from django.contrib import admin

from apps.printing.models import (
    BranchAgent,
    Printer,
    PrintJob,
    PrintJobReconciliation,
    PrintUploadGrant,
)


@admin.register(Printer)
class PrinterAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "model_name", "is_active", "created_at")
    list_filter = ("is_active", "branch")
    search_fields = ("name", "model_name")


@admin.register(BranchAgent)
class BranchAgentAdmin(admin.ModelAdmin):
    list_display = ("name", "branch", "last_seen_at", "revoked_at", "created_at")
    list_filter = ("branch",)
    search_fields = ("name",)
    # A token digest is still authentication material and has no operational UI
    # purpose. Keep it out of HTML entirely; the raw token is never stored.
    exclude = ("token_hash",)
    readonly_fields = ("last_seen_at", "created_at")


@admin.register(PrintJob)
class PrintJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "source_id",
        "status",
        "branch",
        "pages",
        "copies",
        "attempts",
        "created_at",
    )
    list_filter = ("status", "source", "branch")
    # Object-store keys are capability inputs, not operator search terms.
    search_fields = ("source_id",)

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PrintJobReconciliation)
class PrintJobReconciliationAdmin(admin.ModelAdmin):
    list_display = ("id", "job", "branch", "outcome", "resolved_by", "resolved_at")
    list_filter = ("outcome", "branch")
    search_fields = ("job__id", "evidence_reference")

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PrintUploadGrant)
class PrintUploadGrantAdmin(admin.ModelAdmin):
    list_display = ("id", "branch", "requested_by", "expires_at", "consumed_at", "created_at")
    list_filter = ("branch",)
    # Object keys are capability inputs and never belong in rendered admin HTML.
    exclude = ("key", "durable_key")

    def get_readonly_fields(self, request, obj=None):
        return tuple(
            field.name for field in self.model._meta.fields if field.name not in {"key", "durable_key"}
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
