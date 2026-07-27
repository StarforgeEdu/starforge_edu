"""AuditService — the layered read facade over the audit timeline.

Read-only: the write side (``audit_log`` and friends) lives in the domain module
``apps.audit.services`` (imported across the codebase). This service backs the API
list / retrieve / CSV export, all through one filtered timeline.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.audit.dto.audit_dto import AuditFilterDTO
from apps.audit.interfaces.repositories import IAuditRepository
from apps.audit.interfaces.services import IAuditService
from apps.audit.models import AuditLog


class AuditService(IAuditService):
    def __init__(self, logs: IAuditRepository) -> None:
        self._logs = logs

    def filtered(self, filters: AuditFilterDTO) -> QuerySet[AuditLog]:
        return self._logs.filtered(filters)

    def get(self, pk: int) -> AuditLog | None:
        return self._logs.get_by_id(pk)
