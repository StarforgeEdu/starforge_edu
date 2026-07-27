"""ORM-backed audit repository (read-only append-only timeline).

Folds in the former ``apps.audit.selectors``: ``select_related("actor")`` keeps the
list at a fixed query budget, and the filter set (actor / action / resource_type /
resource_id / ts range) is applied here so the API list and the CSV export share one
scoping path.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.audit.dto.audit_dto import AuditFilterDTO
from apps.audit.interfaces.repositories import IAuditRepository
from apps.audit.models import AuditLog
from core.repositories import BaseRepository


class AuditRepository(BaseRepository[AuditLog], IAuditRepository):
    model = AuditLog

    def get_queryset(self) -> QuerySet[AuditLog]:
        return AuditLog.objects.select_related("actor").order_by("-created_at", "-id")

    def timeline(self) -> QuerySet[AuditLog]:
        return self.get_queryset()

    def filtered(self, filters: AuditFilterDTO) -> QuerySet[AuditLog]:
        qs = self.get_queryset()
        if filters.actor is not None:
            qs = qs.filter(actor_id=filters.actor)
        if filters.action:
            qs = qs.filter(action=filters.action)
        if filters.resource_type:
            qs = qs.filter(resource_type=filters.resource_type)
        if filters.resource_id:
            qs = qs.filter(resource_id=str(filters.resource_id))
        if filters.ts_from is not None:
            qs = qs.filter(created_at__gte=filters.ts_from)
        if filters.ts_to is not None:
            qs = qs.filter(created_at__lte=filters.ts_to)
        return qs
