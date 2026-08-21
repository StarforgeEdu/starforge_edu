"""Audit repository port. Read-only, eager-loaded, append-only timeline ordered
``(-created_at, -id)`` — one scoping path shared by the API list + the CSV export."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.audit.dto.audit_dto import AuditFilterDTO, AuditVisibilityDTO
from apps.audit.models import AuditLog
from core.interfaces import IBaseRepository


class IAuditRepository(IBaseRepository[AuditLog]):
    def filtered(
        self,
        filters: AuditFilterDTO,
        visibility: AuditVisibilityDTO,
    ) -> QuerySet[AuditLog]:
        """The timeline narrowed by the shared filter (actor / action / resource / ts range)."""
        raise NotImplementedError

    def get_visible(self, pk: int, visibility: AuditVisibilityDTO) -> AuditLog | None:
        """Return one row only when it falls within the same list visibility."""
        raise NotImplementedError
