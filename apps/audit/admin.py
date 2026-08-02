"""Read-only audit admin (D3-D-1).

The audit trail is immutable: the admin exposes search/filter for incident
review but disallows add / change / delete so even a superuser using Django
admin cannot mutate the append-only log.
"""

from __future__ import annotations

from django.contrib import admin

from apps.audit.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "action",
        "resource_type",
        "resource_id",
        "scope_status",
        "scope_branch_id",
        "scope_department_id",
        "actor_attribution_status",
        "actor_principal_kind",
        "actor_principal_id",
        "actor_repr",
        "ip",
    )
    list_filter = ("scope_status", "actor_attribution_status", "action", "resource_type")
    search_fields = ("actor_repr", "resource_type", "resource_id", "ip", "user_agent")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)

    def has_module_permission(self, request) -> bool:  # pragma: no cover
        return bool(request.user.is_superuser)

    def has_view_permission(self, request, obj=None) -> bool:  # pragma: no cover
        # Tenant-scoped operators use the audited API, whose exact membership
        # boundaries are enforced by the repository. The Django admin is
        # organization-wide and therefore reserved for a real superuser.
        return bool(request.user.is_superuser)

    def has_add_permission(self, request) -> bool:  # pragma: no cover
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # pragma: no cover
        return False

    def has_delete_permission(self, request, obj=None) -> bool:  # pragma: no cover
        return False
