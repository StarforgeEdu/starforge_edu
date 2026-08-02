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

from apps.reports.authorization import (
    ORGANIZATION_ONLY_REPORTS,
    compatible_membership_scopes,
    domain_permission,
    has_organization_scope,
)
from core.permissions import PermissionRoleSet, Role, has_permission_code
from core.spreadsheets import safe_cell

# Plain-role callers exist only in direct generator tests. Runtime callers use
# canonical account kinds from PermissionRoleSet membership scopes.
_LEGACY_STAFF_ROLES = set(Role.ALL) - {Role.TEACHER, Role.STUDENT, Role.PARENT}

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


def teacher_cohort_ids(user) -> set[int]:
    """Cohort ids a teacher owns: primary teacher, co-teacher, or lesson teacher.

    One query. Used by the cohort-scoped generators to restrict a non-staff
    teacher's report to their own cohorts (D4-LB-5 selector scoping).
    """
    from apps.cohorts.selectors import taught_cohorts

    qs = taught_cohorts(user=user)
    return set(qs.values_list("id", flat=True).distinct())


def _legacy_membership_boundaries(*, user, roles: set[str]) -> list[tuple[int, int | None]]:
    """Compatibility boundaries without borrowing unrelated role assignments."""
    if user is None:
        return []
    from django.db.models import Q

    return list(
        user.role_memberships.filter(revoked_at__isnull=True, role__in=roles)
        .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        .values_list("branch_id", "department_id")
    )


def report_generation_is_authorized(*, report_key: str, user, roles: set[str]) -> bool:
    """Fail-closed generator gate independent of the request/service layer."""
    if user is None or not getattr(user, "is_active", False):
        return False
    if bool(getattr(user, "is_superuser", False)):
        return domain_permission(report_key) is not None
    if isinstance(roles, PermissionRoleSet):
        return bool(
            compatible_membership_scopes(
                roles=roles,
                report_key=report_key,
                report_permission="reports:write",
            )
        )
    source_permission = domain_permission(report_key)
    if source_permission is None:
        return False
    role_set = set(roles)
    if report_key in ORGANIZATION_ONLY_REPORTS and Role.DIRECTOR not in role_set:
        return False
    return has_permission_code(role_set, "reports:write") and has_permission_code(role_set, source_permission)


def assert_report_generation_authorized(*, report_key: str, user, roles: set[str]) -> None:
    if report_generation_is_authorized(report_key=report_key, user=user, roles=roles):
        return
    from core.exceptions import PermissionException

    raise PermissionException(code="report_forbidden")


def _scope_q_for_memberships(*, memberships, branch_field: str, department_field: str | None):
    from django.db.models import Q

    if any(membership.is_organization_wide for membership in memberships):
        return Q(pk__isnull=False)
    scoped = Q(pk__in=[])
    for membership in memberships:
        if membership.department_id is None or department_field is None:
            scoped |= Q(**{branch_field: membership.branch_id})
        else:
            scoped |= Q(
                **{
                    branch_field: membership.branch_id,
                    department_field: membership.department_id,
                }
            )
    return scoped


def staff_report_scope_q(
    *,
    report_key: str,
    roles: set[str],
    user,
    branch_field: str,
    department_field: str | None = None,
    report_permission: str = "reports:write",
):
    """Per-membership report scope for custom STAFF account types.

    Plain role sets are retained for direct generator tests/internal callers.
    Runtime ``PermissionRoleSet`` values bind both the reports and source-domain
    grants to the same exact staff membership.
    """
    from django.db.models import Q

    if isinstance(roles, PermissionRoleSet):
        memberships = compatible_membership_scopes(
            roles=roles,
            report_key=report_key,
            report_permission=report_permission,
            account_kinds={"staff"},
        )
        return _scope_q_for_memberships(
            memberships=memberships,
            branch_field=branch_field,
            department_field=department_field,
        )
    if set(roles) & _LEGACY_STAFF_ROLES and report_generation_is_authorized(
        report_key=report_key, user=user, roles=roles
    ):
        visible = Q(pk__in=[])
        for branch_id, department_id in _legacy_membership_boundaries(
            user=user, roles=set(roles) & _LEGACY_STAFF_ROLES
        ):
            if department_id is None:
                visible |= Q(**{branch_field: branch_id})
            elif department_field is not None:
                visible |= Q(
                    **{
                        branch_field: branch_id,
                        department_field: department_id,
                    }
                )
        return visible
    return Q(pk__in=[])


def teacher_report_scope_q(
    *,
    report_key: str,
    roles: set[str],
    user,
    branch_field: str,
    department_field: str | None,
    cohort_field: str,
    report_permission: str = "reports:write",
):
    """Natural teacher scope intersected with the exact compound grant.

    Omitting ``cohort_id`` never expands a teacher to their membership branch;
    the result remains their taught cohorts. A grant in a different branch or
    department cannot be borrowed.
    """
    from django.db.models import Q

    taught = teacher_cohort_ids(user)
    if not taught:
        return Q(pk__in=[])
    if not isinstance(roles, PermissionRoleSet):
        if Role.TEACHER not in set(roles) or not report_generation_is_authorized(
            report_key=report_key, user=user, roles=roles
        ):
            return Q(pk__in=[])
        visible = Q(pk__in=[])
        for branch_id, department_id in _legacy_membership_boundaries(user=user, roles={Role.TEACHER}):
            boundary = {f"{cohort_field}__in": taught, branch_field: branch_id}
            if department_id is not None:
                if department_field is None:
                    continue
                boundary[department_field] = department_id
            visible |= Q(**boundary)
        return visible

    memberships = compatible_membership_scopes(
        roles=roles,
        report_key=report_key,
        report_permission=report_permission,
        account_kinds={"teacher"},
    )
    visible = Q(pk__in=[])
    for membership in memberships:
        boundary = {
            f"{cohort_field}__in": taught,
            branch_field: membership.branch_id,
        }
        if membership.department_id is not None:
            if department_field is None:
                # A department grant cannot safely expand across a source with
                # no department attribution.
                continue
            boundary[department_field] = membership.department_id
        visible |= Q(**boundary)
    return visible


def is_full_scope(
    *, user, roles: set[str], report_key: str, report_permission: str = "reports:write"
) -> bool:
    """Whether the exact compound report grant is organization-wide."""
    return has_organization_scope(
        roles=roles,
        report_key=report_key,
        report_permission=report_permission,
        is_superuser=bool(getattr(user, "is_active", False) and getattr(user, "is_superuser", False)),
    )


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
