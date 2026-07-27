"""AI-usage generator (D4-LB-3): AI tokens consumed in a month.

Consumes Lane A's published interface ``apps.ai.selectors.tokens_consumed(start,
end) -> int`` (cross-app, imported LAZILY). Until Lane A merges the selector,
this tolerates its absence and reports 0 (the WORKLOG D4-LA-9 contract).

Param: ``month`` = "YYYY-MM" (defaults to the current month).
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from typing import Any

from django.utils import timezone

from apps.reports.generators.base import ReportGenerator

logger = logging.getLogger("starforge.reports")


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
    """Call Lane A's selector lazily; tolerate its absence (0) until A merges."""
    try:
        from apps.ai.selectors import tokens_consumed
    except Exception:
        return 0
    try:
        return int(tokens_consumed(start, end))
    except Exception:  # pragma: no cover - defensive while Lane A stabilizes
        logger.exception("tokens_consumed failed in ai_usage report")
        return 0


class AiUsageGenerator(ReportGenerator):
    key = "ai_usage"
    title = "AI usage report"
    template_base = "ai_usage"

    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
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
