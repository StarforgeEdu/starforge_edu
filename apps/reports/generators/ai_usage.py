"""AI-usage generator (D4-LB-3): AI tokens consumed in a month.

Consumes the published interface ``apps.ai.selectors.tokens_consumed(start,
end) -> int`` (cross-app, imported lazily). Source failures abort generation;
they are never rendered as a misleading zero-usage report.

Param: ``month`` = "YYYY-MM" (defaults to the current month).
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from django.utils import timezone

from apps.reports.generators.base import ReportGenerator, assert_report_generation_authorized


def _month_bounds(month: str | None) -> tuple[date, date]:
    """Return (first_day, last_day) for a 'YYYY-MM' string (current month if
    unset/invalid)."""
    today = timezone.localdate()
    year, mon = today.year, today.month
    if month:
        try:
            year, mon = (int(part) for part in month.split("-", 1))
        except (TypeError, ValueError):
            year, mon = today.year, today.month
    last_day = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last_day)


def _tokens_consumed(start: date, end: date) -> int:
    """Call the authoritative selector; failures must not masquerade as zero."""
    from apps.ai.selectors import tokens_consumed

    return int(tokens_consumed(start, end))


class AiUsageGenerator(ReportGenerator):
    key = "ai_usage"
    title = "AI usage report"
    template_base = "ai_usage"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        assert_report_generation_authorized(report_key=self.key, user=user, roles=roles)
        start, end = _month_bounds(params.get("month"))
        total = _tokens_consumed(start, end)
        rows = [
            {
                "period": f"{start.isoformat()}..{end.isoformat()}",
                "tokens_consumed": total,
            }
        ]
        return {
            "columns": ["period", "tokens_consumed"],
            "rows": rows,
            "month": start.strftime("%Y-%m"),
            "tokens_consumed": total,
        }
