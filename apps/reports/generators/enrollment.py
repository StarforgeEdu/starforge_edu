"""Enrollment generator (D4-LB-3): active/enrolled students by branch + cohort.

Optional params: ``branch_id``, ``cohort_id``. Teachers are scoped to their own
cohorts (selector-level, D4-LB-5).
"""

from __future__ import annotations

from typing import Any

from apps.reports.generators.base import (
    ReportGenerator,
    assert_report_generation_authorized,
    enforce_report_row_cap,
    is_full_scope,
    staff_report_scope_q,
    teacher_report_scope_q,
)
from apps.students.models import StudentProfile

_SEAT_STATUSES = (StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE)


class EnrollmentGenerator(ReportGenerator):
    key = "enrollment"
    title = "Enrollment report"
    template_base = "enrollment"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        assert_report_generation_authorized(report_key=self.key, user=user, roles=roles)
        qs = (
            StudentProfile.objects.filter(status__in=_SEAT_STATUSES)
            .select_related("user", "branch", "current_cohort")
            .order_by("branch__name", "student_id")
        )
        if params.get("branch_id"):
            qs = qs.filter(branch_id=params["branch_id"])
        if params.get("cohort_id"):
            qs = qs.filter(current_cohort_id=params["cohort_id"])

        if not is_full_scope(user=user, roles=roles, report_key=self.key):
            visible = staff_report_scope_q(
                report_key=self.key,
                roles=roles,
                user=user,
                branch_field="branch_id",
                department_field="current_cohort__department_id",
            )
            visible |= teacher_report_scope_q(
                report_key=self.key,
                roles=roles,
                user=user,
                branch_field="branch_id",
                department_field="current_cohort__department_id",
                cohort_field="current_cohort_id",
            )
            qs = qs.filter(visible).distinct()

        enforce_report_row_cap(qs)
        rows: list[dict[str, str]] = [
            {
                "student_id": s.student_id,
                "name": s.get_full_name() or s.username or "",
                "status": s.status,
                "branch": s.branch.name if s.branch else "",
                "cohort": s.current_cohort.name if s.current_cohort else "",
                "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else "",
            }
            for s in qs
        ]
        by_status: dict[str, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {
            "columns": ["student_id", "name", "status", "branch", "cohort", "enrollment_date"],
            "rows": rows,
            "total": len(rows),
            "by_status": by_status,
        }
