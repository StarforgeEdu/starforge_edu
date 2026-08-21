"""Billing admin (PUBLIC schema). Read-mostly platform operations surface."""

from __future__ import annotations

from django.contrib import admin

from apps.billing.models import AiUsageCharge, Plan, Subscription, UsageSnapshot
from core.admin_mixins import ReadOnlyAdmin


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "max_students", "max_branches", "storage_gb", "price_uzs", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("price_uzs",)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("center", "plan", "status", "current_period_start", "current_period_end")
    list_filter = ("status", "plan")
    search_fields = ("center__name", "center__slug")
    raw_id_fields = ("center", "plan")
    ordering = ("center_id",)


@admin.register(UsageSnapshot)
class UsageSnapshotAdmin(admin.ModelAdmin):
    list_display = ("center", "date", "students_count", "storage_bytes", "ai_tokens_used")
    list_filter = ("date",)
    search_fields = ("center__name", "center__slug")
    date_hierarchy = "date"
    ordering = ("-date",)


@admin.register(AiUsageCharge)
class AiUsageChargeAdmin(ReadOnlyAdmin):
    list_display = (
        "center",
        "period",
        "included_tokens",
        "used_tokens",
        "overage_tokens",
        "amount_uzs",
        "cost_microusd",
    )
    list_filter = ("period",)
    search_fields = ("center__name", "center__slug")
    date_hierarchy = "period"
    ordering = ("-period", "center_id")
