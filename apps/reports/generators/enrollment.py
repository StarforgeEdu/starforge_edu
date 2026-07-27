"""Enrollment generator (D4-LB-3): active/enrolled students by branch + cohort.

Optional params: ``branch_id``, ``cohort_id``. Teachers are scoped to their own
cohorts (selector-level, D4-LB-5).
"""

from __future__ import annotations

from typing import Any

from apps.reports.generators.base import (
    ReportGenerator,
    enforce_report_row_cap,
    is_full_scope,
    teacher_cohort_ids,
)
from apps.students.models import StudentProfile

_SEAT_STATUSES = (StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE)


class EnrollmentGenerator(ReportGenerator):
    key = "enrollment"
    title = "Enrollment report"
    template_base = "enrollment"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        qs = (
            StudentProfile.objects.filter(status__in=_SEAT_STATUSES)
            .select_related("user", "branch", "current_cohort")
            .order_by("branch__name", "student_id")
        )
        if params.get("branch_id"):
            qs = qs.filter(branch_id=params["branch_id"])
        if params.get("cohort_id"):
            qs = qs.filter(current_cohort_id=params["cohort_id"])

        if not is_full_scope(user=user, roles=roles):
            # Teacher (or any non-staff) sees only students in cohorts they own.
            qs = qs.filter(current_cohort_id__in=teacher_cohort_ids(user))

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
