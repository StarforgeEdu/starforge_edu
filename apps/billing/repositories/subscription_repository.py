"""Subscription repository — the ORM touchpoint for subscription reads."""

from __future__ import annotations

from django.db.models import QuerySet

from apps.billing.interfaces.repositories import ISubscriptionRepository
from apps.billing.models import Subscription
from core.repositories import BaseRepository


class SubscriptionRepository(BaseRepository[Subscription], ISubscriptionRepository):
    model = Subscription

    def query(self) -> QuerySet[Subscription]:
        return Subscription.objects.select_related("plan", "center").all()

    def by_center(self, center_id: int) -> Subscription | None:
        return self.query().filter(center_id=center_id).first()

    def by_pk(self, pk: int) -> Subscription | None:
        return self.query().filter(pk=pk).first()
