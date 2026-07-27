"""Intelligence-domain service port.

Model-less (A-3): the read layer is apps.intelligence.selectors over other apps'
data; the service assembles each transparent facet's response payload. Request-bound
scoping (which students/branches/teachers the caller may see) stays in the view and
is passed in as an already-scoped queryset.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet


class IIntelligenceService(ABC):
    @abstractmethod
    def risk_list(self, *, students: QuerySet, include_finance: bool) -> dict[str, Any]: ...

    @abstractmethod
    def risk_detail(self, *, student, include_finance: bool) -> dict[str, Any]: ...

    @abstractmethod
    def branch_ranking(self, *, branches: QuerySet, include_finance: bool) -> dict[str, Any]: ...

    @abstractmethod
    def family_health(self, *, branches: QuerySet, include_finance: bool) -> dict[str, Any]: ...

    @abstractmethod
    def student_journey(self, *, student, include_finance: bool) -> dict[str, Any]: ...

    @abstractmethod
    def teacher_engagement(self, *, teachers: QuerySet) -> dict[str, Any]: ...

    @abstractmethod
    def rules(self) -> dict[str, Any]: ...
