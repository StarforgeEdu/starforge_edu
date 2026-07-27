"""Platform (apex) Django admin (D4-LE-7) — public-schema staff only (TD-3).

Center list gains latest-snapshot usage columns + a Subscription inline so a
platform operator sees a center's plan/state and last metered usage at a glance.
Apex `/admin/` authenticates only public-schema staff: a tenant-schema user's
credentials do not exist in the public users table, so login fails (asserted by
a test).
"""

from __future__ import annotations

from django.contrib import admin
from django_tenants.admin import TenantAdminMixin

from apps.billing.models import Subscription

from .models import Center, Domain, PlatformEvent


class SubscriptionInline(admin.StackedInline):
    """A Center's subscription, inline on the Center change page (D4-LE-7)."""

    model = Subscription
    extra = 0
    can_delete = False
    fields = ("plan", "status", "current_period_start", "current_period_end")
    readonly_fields = ("current_period_start", "current_period_end")
    raw_id_fields = ("plan",)


@admin.register(Center)
class CenterAdmin(TenantAdminMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "schema_name",
        "slug",
        "is_active",
        "on_trial",
        "subscription_status",
        "latest_students",
        "latest_storage_bytes",
        "latest_ai_tokens",
        "created_at",
    )
    list_filter = ("is_active", "on_trial")
    search_fields = ("name", "slug", "schema_name", "contact_email")
    readonly_fields = ("created_at", "updated_at")
    inlines = (SubscriptionInline,)

    @admin.display(description="Subscription")
    def subscription_status(self, obj: Center) -> str:
        sub = getattr(obj, "subscription", None)
        return sub.status if sub else "—"

    def _latest_snapshot(self, obj: Center):
        return obj.usage_snapshots.order_by("-date").first()

    @admin.display(description="Students")
    def latest_students(self, obj: Center) -> int | str:
        snap = self._latest_snapshot(obj)
        return snap.students_count if snap else "—"

    @admin.display(description="Storage (bytes)")
    def latest_storage_bytes(self, obj: Center) -> int | str:
        snap = self._latest_snapshot(obj)
        return snap.storage_bytes if snap else "—"

    @admin.display(description="AI tokens")
    def latest_ai_tokens(self, obj: Center) -> int | str:
        snap = self._latest_snapshot(obj)
        return snap.ai_tokens_used if snap else "—"


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ("domain", "tenant", "is_primary")
    search_fields = ("domain",)
    list_filter = ("is_primary",)


@admin.register(PlatformEvent)
class PlatformEventAdmin(admin.ModelAdmin):
    """Append-only platform audit trail — read-only in admin (no add/change/delete)."""

    list_display = ("created_at", "event", "center", "actor")
    list_filter = ("event",)
    search_fields = ("center__name", "center__slug", "actor__username")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    readonly_fields = ("actor", "center", "event", "payload", "created_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
