"""Admin for SMS campaigns.

``Campaign`` / ``DoNotContact`` / ``MessageTemplate`` are staff-managed and stay
editable (the send counters are service-owned, so they're read-only). A
``CampaignRecipient`` is NOT hand-authored — the campaign build freezes one row
per matching student (the "who did we contact, with what, and did it land" audit
trail), and the send pass stamps each as sent/failed. So it is view-only here;
the "Add campaign recipient" form the generic auto-admin was exposing never made
sense.
"""

from __future__ import annotations

from django.contrib import admin

from apps.campaigns.models import (
    Campaign,
    CampaignRecipient,
    DoNotContact,
    MessageTemplate,
)
from core.admin_mixins import ReadOnlyAdmin


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "status",
        "branch",
        "total",
        "sent_count",
        "failed_count",
        "skipped_count",
        "scheduled_at",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("name", "message")
    date_hierarchy = "created_at"
    readonly_fields = (
        "total",
        "sent_count",
        "failed_count",
        "skipped_count",
        "sent_by",
        "sent_at",
        "created_at",
        "updated_at",
    )


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(ReadOnlyAdmin):
    list_display = ("campaign", "student", "phone", "status", "sent_at", "error", "created_at")
    list_filter = ("status",)
    search_fields = ("phone", "student__student_id", "campaign__name")
    date_hierarchy = "created_at"


@admin.register(DoNotContact)
class DoNotContactAdmin(admin.ModelAdmin):
    list_display = ("phone", "reason", "created_by", "created_at")
    search_fields = ("phone", "reason")
    date_hierarchy = "created_at"


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "is_active", "created_by", "created_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "purpose", "body")
