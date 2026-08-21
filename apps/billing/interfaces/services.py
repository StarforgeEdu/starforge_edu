"""Service port for the billing (platform monetization) app."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from django.db.models import QuerySet

from apps.billing.models import AiUsageCharge, Plan, Subscription, UsageSnapshot


class IBillingService(ABC):
    # --- plans ---
    @abstractmethod
    def plans(self) -> QuerySet[Plan]: ...

    @abstractmethod
    def plan(self, pk: int) -> Plan | None: ...

    # --- subscriptions ---
    @abstractmethod
    def subscription_by_center(self, center_id: int) -> Subscription | None: ...

    @abstractmethod
    def subscriptions(self) -> QuerySet[Subscription]: ...

    @abstractmethod
    def subscription_by_pk(self, pk: int) -> Subscription | None: ...

    @abstractmethod
    def change_subscription(
        self, *, center_id: int, plan_code: str | None, status: str | None
    ) -> Subscription: ...

    @abstractmethod
    def change_platform_subscription(
        self, *, sub: Subscription, plan_code: str | None, status: str | None, actor: Any
    ) -> Subscription: ...

    # --- usage / charges (read-only, via kept selectors) ---
    @abstractmethod
    def usage(self, *, center_id: int) -> QuerySet[UsageSnapshot]: ...

    @abstractmethod
    def ai_charges(self, *, center_id: int) -> QuerySet[AiUsageCharge]: ...

    # --- checkout ---
    @abstractmethod
    def checkout(self, *, center_id: int, provider: str) -> Subscription: ...
