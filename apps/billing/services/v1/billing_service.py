"""Billing platform service — thin orchestration over the preserved billing
domain functions + the plan/subscription read repositories.

The subscription state machine, plan-limit enforcement, dunning, metering, and
platform checkout stay VERBATIM in ``apps.billing.services`` (the package
__init__), which is imported by students (enforce_student_limit), tenancy
(change_subscription), and the celery billing tasks. This class adapts the
control-center view surface to the layered/DI style and, on a flat-subscription
PATCH, records the D4-LE-5 PlatformEvent (as the old viewset did).
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.billing import selectors as billing_selectors
from apps.billing import services as billing_domain
from apps.billing.interfaces.repositories import IPlanRepository, ISubscriptionRepository
from apps.billing.interfaces.services import IBillingService
from apps.billing.models import AiUsageCharge, Plan, Subscription, UsageSnapshot


class BillingService(IBillingService):
    def __init__(
        self, plan_repository: IPlanRepository, subscription_repository: ISubscriptionRepository
    ) -> None:
        self._plans = plan_repository
        self._subs = subscription_repository

    # --- plans ---
    def plans(self) -> QuerySet[Plan]:
        return self._plans.query()

    def plan(self, pk: int) -> Plan | None:
        return self._plans.get(pk)

    # --- subscriptions ---
    def subscription_by_center(self, center_id: int) -> Subscription | None:
        return self._subs.by_center(center_id)

    def subscriptions(self) -> QuerySet[Subscription]:
        return self._subs.query()

    def subscription_by_pk(self, pk: int) -> Subscription | None:
        return self._subs.by_pk(pk)

    def change_subscription(
        self, *, center_id: int, plan_code: str | None, status: str | None
    ) -> Subscription:
        return billing_domain.change_subscription(center_id=center_id, plan_code=plan_code, status=status)

    def change_platform_subscription(
        self, *, sub: Subscription, plan_code: str | None, status: str | None, actor: Any
    ) -> Subscription:
        old_status = sub.status
        updated = billing_domain.change_subscription(
            center_id=sub.center_id, plan_code=plan_code, status=status
        )
        # D4-LE-5: a control-center subscription change is a PlatformEvent (the
        # tenant-side AuditLog is written by change_subscription). tenancy owns the
        # public-schema audit trail.
        from apps.tenancy.services import PlatformEvent, record_platform_event

        record_platform_event(
            actor=actor,
            center=updated.center,
            event=PlatformEvent.Event.SUBSCRIPTION_CHANGED,
            payload={"old_status": old_status, "new_status": updated.status, "plan_code": plan_code},
        )
        return updated

    # --- usage / charges (read-only, via the kept selectors) ---
    def usage(self, *, center_id: int) -> QuerySet[UsageSnapshot]:
        return billing_selectors.usage_for_center(center_id=center_id)

    def ai_charges(self, *, center_id: int) -> QuerySet[AiUsageCharge]:
        return billing_selectors.ai_charges_for_center(center_id=center_id)

    # --- checkout ---
    def checkout(self, *, center_id: int, provider: str) -> Subscription:
        return billing_domain.process_platform_checkout(center_id=center_id, provider=provider)
