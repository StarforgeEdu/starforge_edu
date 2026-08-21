"""Schedule-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

from datetime import date

import factory

from apps.schedule.models import Term


class TermFactory(factory.django.DjangoModelFactory[Term]):
    class Meta:
        model = Term
        django_get_or_create = ("academic_year", "name")

    name = factory.Sequence(lambda n: f"Term {n}")
    academic_year = "2026-2027"
    start_date = date(2026, 1, 1)
    end_date = date(2026, 12, 31)
