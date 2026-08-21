"""Plan repository — the ORM touchpoint for the plan catalog reads."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.billing.interfaces.repositories import IPlanRepository
from apps.billing.models import Plan
from core.repositories import BaseRepository


class PlanRepository(BaseRepository[Plan], IPlanRepository):
    model = Plan

    def query(self) -> QuerySet[Plan]:
        return Plan.objects.all()

    def get(self, pk: int) -> Plan | None:
        return Plan.objects.filter(pk=pk).first()
