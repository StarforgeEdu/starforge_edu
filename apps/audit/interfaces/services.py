"""Audit read-side service port."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.audit.dto.audit_dto import AuditFilterDTO
from apps.audit.models import AuditLog


class IAuditService(ABC):
    @abstractmethod
    def filtered(self, filters: AuditFilterDTO) -> QuerySet[AuditLog]: ...

    @abstractmethod
    def get(self, pk: int) -> AuditLog | None: ...
