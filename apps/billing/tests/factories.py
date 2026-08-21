"""Billing factories (TESTING.md §4). Public-schema models — these do NOT need a
schema_context wrapper (Plan/Subscription/UsageSnapshot live in public)."""

from __future__ import annotations

from decimal import Decimal

import factory
from django.utils import timezone

from apps.billing.models import Plan, Subscription, UsageSnapshot


class PlanFactory(factory.django.DjangoModelFactory[Plan]):
    class Meta:
        model = Plan
        django_get_or_create = ("code",)

    code = factory.Sequence(lambda n: f"plan-{n}")
    name = factory.Sequence(lambda n: f"Plan {n}")
    max_students = 100
    max_branches = 2
    ai_tokens_month = 100_000
    storage_gb = 10
    price_uzs = Decimal("1000000")
    is_active = True


class SubscriptionFactory(factory.django.DjangoModelFactory[Subscription]):
    class Meta:
        model = Subscription

    plan = factory.SubFactory(PlanFactory)
    status = Subscription.Status.ACTIVE
    current_period_start = factory.LazyFunction(timezone.now)
    current_period_end = factory.LazyFunction(lambda: timezone.now() + timezone.timedelta(days=30))


class UsageSnapshotFactory(factory.django.DjangoModelFactory[UsageSnapshot]):
    class Meta:
        model = UsageSnapshot

    date = factory.LazyFunction(lambda: timezone.now().date())
    students_count = 0
    storage_bytes = 0
    ai_tokens_used = 0
