"""Repository ports for the billing (platform monetization) app."""

from __future__ import annotations

from abc import ABC, abstractmethod

from django.db.models import QuerySet

from apps.billing.models import Plan, Subscription


class IPlanRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[Plan]:
        """Base Plan catalog queryset (Meta.ordering by price)."""

    @abstractmethod
    def get(self, pk: int) -> Plan | None: ...


class ISubscriptionRepository(ABC):
    @abstractmethod
    def query(self) -> QuerySet[Subscription]:
        """All subscriptions (plan + center select_related) for the flat list view."""

    @abstractmethod
    def by_center(self, center_id: int) -> Subscription | None: ...

    @abstractmethod
    def by_pk(self, pk: int) -> Subscription | None: ...
