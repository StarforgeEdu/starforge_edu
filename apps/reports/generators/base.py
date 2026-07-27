"""Generator base protocol + shared scoping/render helpers (D4-LB-3).

A generator separates three concerns:

* ``collect(params, *, user, roles)`` — a *pure* selector. Eager-loads with
  ``select_related``/``prefetch_related`` (zero N+1, query-count tested) and
  applies role/cohort scoping IN the selector layer (DAY-4 D4-LB-5: teachers are
  scoped to their own cohorts here, never in the view). Returns a plain JSON-ish
  dict the renderers consume — never a live queryset, so the renderers do no DB.
* ``render_pdf(data)`` — lazy ``weasyprint`` import; renders the locale HTML
  template. weasyprint's GTK native libs are absent on the dev box, so the import
  is deferred to call time (mirrors academics' transcript renderer).
* ``render_xlsx(data)`` — lazy ``openpyxl`` import; one worksheet from the same
  ``data``.

``render(data, fmt, *, locale)`` dispatches on format.
"""

from __future__ import annotations

from typing import Any

from core.permissions import Role

# Roles that see the whole tenant for the cohort-scoped reports (enrollment /
# attendance / grades). A teacher is scoped to cohorts they own (see
# teacher_cohort_ids). Accountants are NOT here — finance scoping is per-report.
STAFF_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT}

# Locale set every report template ships (TD-14).
TEMPLATE_LOCALES = ("uz", "ru", "en")

# A report generator materializes every matching row into a Python list AND an
# in-memory HTML/openpyxl document, so an unbounded result set (a director running
# attendance/grades/enrollment with no date filter over a multi-year center) OOM-kills
# the SHARED tenant Celery worker, taking co-running tenants' tasks down with it.
# Refuse above this many rows (mirrors apps/audit's MAX_EXPORT_ROWS) — the caller
# narrows by date range / cohort. build_report catches the raise and marks the run
# FAILED with the message, instead of flapping on repeated OOMs.
MAX_REPORT_ROWS = 50_000


def enforce_report_row_cap(qs) -> None:
    """Raise a clean ValidationException if ``qs`` would exceed MAX_REPORT_ROWS, before
    the caller materializes it. Counts at the DB (cheap) rather than loading rows."""
    from core.exceptions import ValidationException

    total = qs.count()
    if total > MAX_REPORT_ROWS:
        raise ValidationException(
            "This report matches too many rows; narrow the date range or cohort.",
            code="report_too_large",
            fields={"rows": [f"{total} rows match (max {MAX_REPORT_ROWS})."]},
        )

# Characters that make Excel/LibreOffice treat a cell as a formula. Report cells
# carry tenant-user-controlled strings (student/cohort/library names), so any of
# these as a leading char must be neutralized to block CSV/XLSX formula injection.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """Neutralize a spreadsheet formula-injection vector in a string cell.

    A leading ``= + - @`` (or tab/CR) turns user text into an active formula when
    a director/accountant opens the workbook. Prefix such strings with an
    apostrophe so the spreadsheet renders them as literal text. Non-strings
    (numbers/Decimals/None) pass through unchanged."""
    if isinstance(value, str) and value[:1] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def teacher_cohort_ids(user) -> set[int]:
    """Cohort ids a teacher owns: primary teacher, co-teacher, or lesson teacher.

    One query. Used by the cohort-scoped generators to restrict a non-staff
    teacher's report to their own cohorts (D4-LB-5 selector scoping).
    """
    from django.db.models import Q

    from apps.cohorts.models import Cohort

    qs = Cohort.objects.filter(
        Q(primary_teacher__user=user) | Q(co_teachers__teacher__user=user) | Q(lessons__teacher__user=user)
    )
    return set(qs.values_list("id", flat=True).distinct())


def is_full_scope(*, user, roles: set[str]) -> bool:
    """True when the caller sees the whole tenant (superuser / director / head)."""
    return bool(getattr(user, "is_superuser", False)) or bool(roles & STAFF_ROLES)


def _fallback_locales(locale: str) -> list[str]:
    chain = [locale]
    for fallback in ("uz", "en"):
        if fallback not in chain:
            chain.append(fallback)
    return chain


class ReportGenerator:
    """Base class. Subclasses set ``key``/``title`` and implement ``collect`` +
    ``_xlsx_sheet``; PDF rendering is template-driven via ``template_base``."""

    key: str = ""
    title: str = ""
    # Base name of the HTML template family: documents/reports/<base>_<locale>.html
    template_base: str = ""

    # ------------------------------------------------------------------ collect
    def collect(self, params: dict[str, Any], *, user, roles: set[str]) -> dict[str, Any]:
        raise NotImplementedError

    # ------------------------------------------------------------------- render
    def render(self, data: dict[str, Any], fmt: str, *, locale: str = "uz") -> bytes:
        if fmt == "xlsx":
            return self.render_xlsx(data)
        return self.render_pdf(data, locale=locale)

    def render_pdf(self, data: dict[str, Any], *, locale: str = "uz") -> bytes:
        """Render the locale HTML template to PDF. weasyprint is imported lazily
        (GTK native libs only needed here)."""
        from django.template.loader import select_template
        from django.utils import translation
        from weasyprint import HTML  # lazy: native libs absent on the dev box

        names = [f"documents/reports/{self.template_base}_{loc}.html" for loc in _fallback_locales(locale)]
        with translation.override(locale):
            template = select_template(names)
            html = template.render({"data": data, "report_title": self.title})
        return HTML(string=html).write_pdf()

    def render_xlsx(self, data: dict[str, Any]) -> bytes:
        """Render ``data`` to an .xlsx workbook. openpyxl is imported lazily so
        the app loads where it is not installed (tests skip the render path)."""
        import io

        from openpyxl import Workbook  # lazy: optional dep, not installed locally

        wb = Workbook()
        ws = wb.active
        ws.title = self.key[:31]
        self._xlsx_sheet(ws, data)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def _xlsx_sheet(self, ws, data: dict[str, Any]) -> None:
        """Write rows onto the worksheet. Default: header + each ``rows`` dict."""
        rows = data.get("rows", [])
        columns = data.get("columns") or (list(rows[0].keys()) if rows else [])
        ws.append([safe_cell(str(c)) for c in columns])
        for row in rows:
            ws.append([safe_cell(row.get(c, "")) for c in columns])
