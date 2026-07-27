"""Attendance-domain factories (TESTING.md §4). Call inside schema_context(tenant)."""

from __future__ import annotations

import factory

from apps.attendance.models import AttendanceRecord


class AttendanceRecordFactory(factory.django.DjangoModelFactory[AttendanceRecord]):
    class Meta:
        model = AttendanceRecord

    status = AttendanceRecord.Status.PRESENT
    # `student` and `lesson` are required — pass them explicitly so a record ties
    # to a real cohort/term the test controls (a bare SubFactory would invent an
    # unrelated lesson and break summary/dashboard aggregations).
