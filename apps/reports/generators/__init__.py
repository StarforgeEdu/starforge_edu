"""Report generator library (D4-LB-3).

Each generator is a pure data collector (``collect`` — eager-loaded selector with
role/cohort scoping) plus two renderers (``render_pdf`` via weasyprint, lazy;
``render_xlsx`` via openpyxl, lazy). The registry maps a ``ReportKey`` to its
generator instance; ``get_generator(key)`` is the single lookup the service +
Celery task use.
"""

from __future__ import annotations

from apps.reports.generators.ai_usage import AiUsageGenerator
from apps.reports.generators.attendance import AttendanceGenerator
from apps.reports.generators.base import ReportGenerator
from apps.reports.generators.enrollment import EnrollmentGenerator
from apps.reports.generators.finance import FinanceGenerator
from apps.reports.generators.grades import GradesGenerator
from apps.reports.generators.storage_usage import StorageUsageGenerator
from apps.reports.models import ReportKey
from core.exceptions import ValidationException

_REGISTRY: dict[str, ReportGenerator] = {
    g.key: g
    for g in (
        EnrollmentGenerator(),
        AttendanceGenerator(),
        GradesGenerator(),
        FinanceGenerator(),
        AiUsageGenerator(),
        StorageUsageGenerator(),
    )
}


def get_generator(key: str) -> ReportGenerator:
    """Return the generator for a ReportKey, or raise a 422 for an unknown key."""
    gen = _REGISTRY.get(key)
    if gen is None:
        raise ValidationException(code="unknown_report_key")
    return gen


def all_keys() -> tuple[str, ...]:
    return tuple(ReportKey.values)


__all__ = ["ReportGenerator", "all_keys", "get_generator"]
