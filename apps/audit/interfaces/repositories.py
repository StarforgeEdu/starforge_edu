"""Audit repository port. Read-only, eager-loaded, append-only timeline ordered
``(-created_at, -id)`` — one scoping path shared by the API list + the CSV export."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.audit.dto.audit_dto import AuditFilterDTO
from apps.audit.models import AuditLog
from core.interfaces import IBaseRepository


class IAuditRepository(IBaseRepository[AuditLog]):
    def timeline(self) -> QuerySet[AuditLog]:
        """The full timeline (newest first, actor pre-joined)."""
        raise NotImplementedError

    def filtered(self, filters: AuditFilterDTO) -> QuerySet[AuditLog]:
        """The timeline narrowed by the shared filter (actor / action / resource / ts range)."""
        raise NotImplementedError
