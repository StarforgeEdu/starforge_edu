"""Grades generator (D4-LB-3): published grades per student/subject/term.

Params: optional ``term_id``, ``subject_id``, and ``include_unpublished`` (only
honored for full-scope callers). Teachers are scoped to students who are active
members of cohorts they own (selector-level, D4-LB-5).
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q

from apps.academics.models import Grade
from apps.reports.generators.base import (
    ReportGenerator,
    assert_report_generation_authorized,
    enforce_report_row_cap,
    is_full_scope,
    staff_report_scope_q,
    teacher_report_scope_q,
)


class GradesGenerator(ReportGenerator):
    key = "grades"
    title = "Grades report"
    template_base = "grades"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        assert_report_generation_authorized(report_key=self.key, user=user, roles=roles)
        full = is_full_scope(user=user, roles=roles, report_key=self.key)
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
        if params.get("branch_id"):
            qs = qs.filter(student__branch_id=params["branch_id"])

        if not full:
            visible = staff_report_scope_q(
                report_key=self.key,
                roles=roles,
                user=user,
                branch_field="student__branch_id",
                department_field="student__current_cohort__department_id",
            )
            visible |= teacher_report_scope_q(
                report_key=self.key,
                roles=roles,
                user=user,
                branch_field="student__cohort_memberships__cohort__branch_id",
                department_field="student__cohort_memberships__cohort__department_id",
                cohort_field="student__cohort_memberships__cohort_id",
            ) & Q(student__cohort_memberships__end_date__isnull=True)
            qs = qs.filter(visible).distinct()

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
