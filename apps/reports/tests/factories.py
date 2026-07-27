"""Reports-domain factories (TESTING.md §4). Call inside schema_context(tenant).

The 6 library Report rows are seeded by migration 0003 — fetch them with
``Report.objects.get(key=...)`` rather than creating new ones (key is unique).
"""

from __future__ import annotations

import factory

from apps.reports.models import Report, ReportRun, ReportSchedule


class ReportRunFactory(factory.django.DjangoModelFactory[ReportRun]):
    class Meta:
        model = ReportRun

    report = factory.LazyFunction(lambda: Report.objects.get(key="enrollment"))
    format = "pdf"
    status = ReportRun.Status.QUEUED
    params: dict = {}


class ReportScheduleFactory(factory.django.DjangoModelFactory[ReportSchedule]):
    class Meta:
        model = ReportSchedule

    report = factory.LazyFunction(lambda: Report.objects.get(key="enrollment"))
    cadence = ReportSchedule.Cadence.WEEKLY
    weekday = 0
    hour = 7
    format = "pdf"
    is_active = True
