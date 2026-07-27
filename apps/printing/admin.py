"""Printing admin (D4-LD-1). Read-mostly operational views."""

from __future__ import annotations

from django.contrib import admin

from apps.printing.models import BranchAgent, Printer, PrintJob


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
    # token_hash is never exposed; raw token never stored.
    readonly_fields = ("token_hash", "last_seen_at", "created_at")


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
    search_fields = ("source_id", "payload_s3_key")
    readonly_fields = ("created_at", "claimed_at", "finished_at")
