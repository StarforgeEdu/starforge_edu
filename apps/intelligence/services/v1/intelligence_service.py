"""Intelligence application service — assembles each transparent A-3 facet's
response payload from the preserved apps.intelligence.selectors read layer."""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from apps.intelligence import selectors
from apps.intelligence.interfaces.services import IIntelligenceService


class IntelligenceService(IIntelligenceService):
    def risk_list(self, *, students: QuerySet, include_finance: bool) -> dict[str, Any]:
        results = selectors.student_risk(students, include_finance=include_finance)
        return {"count": len(results), "results": results}

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

    def teacher_engagement(self, *, teachers: QuerySet) -> dict[str, Any]:
        results = selectors.teacher_engagement(teachers)
        return {"count": len(results), "results": results, "metrics": selectors.TEACHER_METRICS}

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
