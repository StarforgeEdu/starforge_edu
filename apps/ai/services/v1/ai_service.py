"""AI application service — wraps the preserved apps.ai.services domain functions
and apps.ai.selectors reads behind the IAIService port."""

from __future__ import annotations

from datetime import date

from django.db import transaction
from django.db.models import QuerySet

from apps.ai import selectors
from apps.ai import services as domain
from apps.ai.interfaces.services import IAIService
from apps.ai.models import AIRequest, TenantAIBudget


class AIService(IAIService):
    def list_requests(self) -> QuerySet[AIRequest]:
        return selectors.list_requests()

    def get_request(self, *, pk: int) -> AIRequest | None:
        return selectors.list_requests().filter(pk=pk).first()

    def get_budget(self) -> TenantAIBudget:
        # _get_budget_locked() uses select_for_update (+ may roll day/month counters
        # over), which REQUIRES an open transaction. Requests run in autocommit (no
        # ATOMIC_REQUESTS), so wrap it or Postgres raises TransactionManagementError.
        with transaction.atomic():
            return domain._get_budget_locked()

    def update_budget(
        self, *, daily_token_limit: int | None, monthly_token_limit: int | None, is_enabled: bool | None
    ) -> TenantAIBudget:
        return domain.update_budget(
            daily_token_limit=daily_token_limit,
            monthly_token_limit=monthly_token_limit,
            is_enabled=is_enabled,
        )

    def request_exam_generation(
        self, *, requested_by, subject_id: int, exam_type: str, question_count: int, difficulty: str
    ) -> AIRequest:
        return domain.request_exam_generation(
            requested_by=requested_by,
            subject_id=subject_id,
            exam_type=exam_type,
            question_count=question_count,
            difficulty=difficulty,
        )

    def usage_report(self, *, start: date, end: date) -> list[dict]:
        return selectors.usage_report(start=start, end=end)
