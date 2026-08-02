"""ORM-backed audit repository (read-only append-only timeline).

Folds in the former ``apps.audit.selectors``: ``select_related("actor")`` keeps the
list at a fixed query budget, and the filter set (actor / action / resource_type /
resource_id / ts range) is applied here so the API list and the CSV export share one
scoping path.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.audit.dto.audit_dto import AuditFilterDTO, AuditVisibilityDTO
from apps.audit.interfaces.repositories import IAuditRepository
from apps.audit.models import AuditLog
from core.repositories import BaseRepository


class AuditRepository(BaseRepository[AuditLog], IAuditRepository):
    model = AuditLog

    def get_queryset(self) -> QuerySet[AuditLog]:
        return AuditLog.objects.select_related("actor").order_by("-created_at", "-id")

    def filtered(
        self,
        filters: AuditFilterDTO,
        visibility: AuditVisibilityDTO,
    ) -> QuerySet[AuditLog]:
        qs = self._visible(self.get_queryset(), visibility)
        if filters.actor is not None:
            qs = qs.filter(actor_id=filters.actor)
        if filters.actor_principal_kind is not None:
            qs = qs.filter(
                actor_attribution_status=AuditLog.ActorAttributionStatus.EXACT,
                actor_principal_kind=filters.actor_principal_kind,
                actor_principal_id=filters.actor_principal_id,
            )
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
        if filters.branch is not None:
            qs = qs.filter(scope_branch_id=filters.branch)
        if filters.department is not None:
            qs = qs.filter(scope_department_id=filters.department)
        if filters.scope_status:
            qs = qs.filter(scope_status=filters.scope_status)
        if filters.sensitivity:
            qs = qs.filter(sensitivity=filters.sensitivity)
        return qs

    def get_visible(self, pk: int, visibility: AuditVisibilityDTO) -> AuditLog | None:
        return self._visible(self.get_queryset(), visibility).filter(pk=pk).first()

    @staticmethod
    def _visible(
        queryset: QuerySet[AuditLog],
        visibility: AuditVisibilityDTO,
    ) -> QuerySet[AuditLog]:
        if visibility.organization_wide:
            visible = queryset
        else:
            boundary = Q(pk__in=[])
            if visibility.branch_wide_ids:
                boundary |= Q(scope_branch_id__in=visibility.branch_wide_ids)
            for branch_id, department_id in visibility.department_scopes:
                boundary |= Q(
                    scope_branch_id=branch_id,
                    scope_department_id=department_id,
                )
            visible = queryset.filter(Q(scope_status=AuditLog.ScopeStatus.SCOPED) & boundary)

        # Salary and payout configuration require a second permission boundary;
        # ``audit:read`` alone must not become a compensation oracle. The
        # immutable classification is enforced by a database INSERT trigger as
        # well as the application writer, including during rolling deploys.
        sensitive = Q(sensitivity=AuditLog.Sensitivity.COMPENSATION)
        if visibility.compensation_organization_wide:
            return visible

        compensation_boundary = Q(pk__in=[])
        if visibility.compensation_branch_wide_ids:
            compensation_boundary |= Q(scope_branch_id__in=visibility.compensation_branch_wide_ids)
        for branch_id, department_id in visibility.compensation_department_scopes:
            compensation_boundary |= Q(
                scope_branch_id=branch_id,
                scope_department_id=department_id,
            )
        compensation_visible = Q(scope_status=AuditLog.ScopeStatus.SCOPED) & compensation_boundary
        return visible.filter(~sensitive | (sensitive & compensation_visible))
