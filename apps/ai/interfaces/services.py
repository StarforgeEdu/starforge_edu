"""AI-domain service port.

Thin application service over the PRESERVED apps.ai.services domain functions
(budget lock/update, exam-generation request) and apps.ai.selectors reads. No
repository: the request log + usage report come from selectors, and the budget
mutation goes through the preserved locked domain functions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from django.db.models import QuerySet

from apps.ai.models import AIRequest, TenantAIBudget


class IAIService(ABC):
    @abstractmethod
    def list_requests(self) -> QuerySet[AIRequest]: ...

    @abstractmethod
    def get_request(self, *, pk: int) -> AIRequest | None: ...

    @abstractmethod
    def get_budget(self) -> TenantAIBudget: ...

    @abstractmethod
    def update_budget(
        self, *, daily_token_limit: int | None, monthly_token_limit: int | None, is_enabled: bool | None
    ) -> TenantAIBudget: ...

    @abstractmethod
    def request_exam_generation(
        self, *, requested_by, subject_id: int, exam_type: str, question_count: int, difficulty: str
    ) -> AIRequest: ...

    @abstractmethod
    def usage_report(self, *, start: date, end: date) -> list[dict]: ...
