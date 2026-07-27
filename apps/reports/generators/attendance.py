"""Attendance generator (D4-LB-3): attendance records + per-status counts.

Params: optional ``cohort_id``, ``date_from``, ``date_to`` (ISO dates). Teachers
are scoped to lessons in their own cohorts (selector-level, D4-LB-5).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone

from apps.attendance.models import AttendanceRecord
from apps.reports.generators.base import (
    ReportGenerator,
    enforce_report_row_cap,
    is_full_scope,
    teacher_cohort_ids,
)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class AttendanceGenerator(ReportGenerator):
    key = "attendance"
    title = "Attendance report"
    template_base = "attendance"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        qs = AttendanceRecord.objects.select_related(
            "student__user", "lesson__cohort", "lesson__teacher__user"
        ).order_by("lesson__starts_at", "student__student_id")
        if params.get("cohort_id"):
            qs = qs.filter(lesson__cohort_id=params["cohort_id"])
        date_from = _parse_date(params.get("date_from"))
        date_to = _parse_date(params.get("date_to"))
        if date_from:
            qs = qs.filter(lesson__starts_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(lesson__starts_at__date__lte=date_to)

        if not is_full_scope(user=user, roles=roles):
            qs = qs.filter(lesson__cohort_id__in=teacher_cohort_ids(user))

        enforce_report_row_cap(qs)
        rows = []
        by_status: dict[str, int] = {}
        for rec in qs:
            status = rec.status
            by_status[status] = by_status.get(status, 0) + 1
            rows.append(
                {
                    "date": timezone.localdate(rec.lesson.starts_at).isoformat() if rec.lesson_id else "",
                    "lesson": rec.lesson.title if rec.lesson_id else "",
                    "cohort": rec.lesson.cohort.name if rec.lesson_id and rec.lesson.cohort_id else "",
                    "student": rec.student.get_full_name() or rec.student.username,
                    "status": status,
                }
            )
        return {
            "columns": ["date", "lesson", "cohort", "student", "status"],
            "rows": rows,
            "total": len(rows),
            "by_status": by_status,
        }
