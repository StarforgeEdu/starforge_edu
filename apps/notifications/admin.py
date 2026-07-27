from django.contrib import admin

from apps.notifications.models import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
)
from core.admin_mixins import ReadOnlyAdmin


class NotificationDeliveryInline(admin.TabularInline):
    """The per-channel delivery outcomes for a notification, read-only (written by
    the fan-out task, not by hand — mirrors the append-only log pattern)."""

    model = NotificationDelivery
    extra = 0
    fields = ("channel", "status", "provider_response", "sent_at", "created_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "event_type", "title", "read_at", "created_at")
    list_filter = ("event_type", "read_at")
    search_fields = ("title", "dedupe_key")
    autocomplete_fields = ("user",)
    list_select_related = ("user",)
    date_hierarchy = "created_at"
    inlines = (NotificationDeliveryInline,)


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(ReadOnlyAdmin):
    """Per-channel delivery attempt outcomes — written by the fan-out task, so
    view-only here (matches the audit/ledger log pattern)."""

    list_display = ("id", "notification", "channel", "status", "sent_at", "created_at")
    list_filter = ("channel", "status")
    autocomplete_fields = ("notification",)
    list_select_related = ("notification",)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "event_type", "channel", "enabled")
    list_filter = ("event_type", "channel", "enabled")
    autocomplete_fields = ("user",)
    list_select_related = ("user",)


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    list_display = ("id", "event_type", "channel", "locale", "is_active")
    list_filter = ("event_type", "channel", "locale", "is_active")
    search_fields = ("subject", "body")
