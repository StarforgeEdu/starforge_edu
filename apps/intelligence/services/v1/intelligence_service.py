"""Intelligence application service — assembles each transparent A-3 facet's
response payload from the preserved apps.intelligence.selectors read layer."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.intelligence import selectors
from apps.intelligence.dto import ExecutiveSummaryContext
from apps.intelligence.interfaces.services import IIntelligenceService


class IntelligenceService(IIntelligenceService):
    def executive_summary(self, *, context: ExecutiveSummaryContext) -> dict[str, Any]:
        return selectors.executive_summary(context)

    def risk_list(
        self,
        *,
        students: QuerySet,
        include_finance: bool,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        results, total = selectors.student_risk_page(
            students,
            include_finance=include_finance,
            page=page,
            page_size=page_size,
        )
        return _page_payload(results, total=total, page=page, page_size=page_size)

    def risk_detail(self, *, student, include_finance: bool) -> dict[str, Any]:
        return selectors.student_risk_detail(student, include_finance=include_finance)

    def branch_ranking(self, *, branches: QuerySet, include_finance: bool) -> dict[str, Any]:
        results = selectors.branch_ranking(branches, include_finance=include_finance)
        return {
            "count": len(results),
            "method": {
                "metrics": selectors.BRANCH_METRICS,
                "score_range": "0-100",
                "min_cell_size": selectors.MIN_BRANCH_CELL,
                "includes_finance": include_finance,
            },
            "results": results,
        }

    def family_health(self, *, branches: QuerySet, include_finance: bool) -> dict[str, Any]:
        results = selectors.family_health(branches, include_finance=include_finance)
        return {"count": len(results), "levels": selectors.FAMILY_HEALTH_LEVELS, "results": results}

    def student_journey(self, *, student, include_finance: bool) -> dict[str, Any]:
        events = selectors.student_journey(student, include_finance=include_finance)
        return {"student": student.id, "events": events}

    def teacher_engagement(
        self,
        *,
        teachers: QuerySet,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        results, total = selectors.teacher_engagement_page(
            teachers,
            page=page,
            page_size=page_size,
        )
        return {
            **_page_payload(results, total=total, page=page, page_size=page_size),
            "metrics": selectors.TEACHER_METRICS,
        }

    def rules(self) -> dict[str, Any]:
        return {
            "rules": selectors.RULES,
            "thresholds": {
                "attendance_window_days": selectors.ATTENDANCE_WINDOW_DAYS,
                "min_lessons": selectors.MIN_LESSONS_FOR_ATTENDANCE_FLAG,
                "absence_rate": selectors.ABSENCE_RATE_THRESHOLD,
                "low_grade_pct": selectors.LOW_GRADE_PCT_THRESHOLD,
            },
            "levels": {"low": "1-2", "medium": "3-4", "high": "5+"},
        }


def _page_payload(
    results: list[dict[str, Any]],
    *,
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    return {
        "count": total,
        "results": results,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }
