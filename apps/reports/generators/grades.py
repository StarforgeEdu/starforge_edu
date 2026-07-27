"""Grades generator (D4-LB-3): published grades per student/subject/term.

Params: optional ``term_id``, ``subject_id``, and ``include_unpublished`` (only
honored for full-scope callers). Teachers are scoped to students who are active
members of cohorts they own (selector-level, D4-LB-5).
"""

from __future__ import annotations

from typing import Any

from apps.academics.models import Grade
from apps.reports.generators.base import (
    ReportGenerator,
    enforce_report_row_cap,
    is_full_scope,
    teacher_cohort_ids,
)


class GradesGenerator(ReportGenerator):
    key = "grades"
    title = "Grades report"
    template_base = "grades"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        full = is_full_scope(user=user, roles=roles)
        qs = Grade.objects.select_related("student__user", "subject", "term").order_by(
            "student__student_id", "subject__name"
        )
        # Publication gate: only full-scope callers may opt into unpublished rows.
        if not (full and params.get("include_unpublished")):
            qs = qs.filter(is_published=True)
        if params.get("term_id"):
            qs = qs.filter(term_id=params["term_id"])
        if params.get("subject_id"):
            qs = qs.filter(subject_id=params["subject_id"])

        if not full:
            cohort_ids = teacher_cohort_ids(user)
            qs = qs.filter(
                student__cohort_memberships__cohort_id__in=cohort_ids,
                student__cohort_memberships__end_date__isnull=True,
            ).distinct()

        enforce_report_row_cap(qs)
        rows = [
            {
                "student": g.student.get_full_name() or g.student.username,
                "student_id": g.student.student_id,
                "subject": g.subject.name,
                "term": g.term.name if g.term_id else "",
                "grade": g.value_display,
                "score": str(g.value_raw),
                "published": g.is_published,
            }
            for g in qs
        ]
        return {
            "columns": ["student_id", "student", "subject", "term", "grade", "score", "published"],
            "rows": rows,
            "total": len(rows),
        }
