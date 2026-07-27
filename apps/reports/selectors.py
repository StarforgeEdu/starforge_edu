"""Reports read-side selectors (D4-LB-5).

Library + run + schedule visibility is role-scoped HERE (not in the view): a
director sees the whole library; an accountant sees only ``finance``; a teacher
sees ``enrollment``/``attendance``/``grades`` (cohort row-scoping inside the
generators). Runs/schedules a caller can see are limited to the reports their
roles can run.
"""

from __future__ import annotations

from django.db.models import QuerySet

from apps.reports.models import Report, ReportRun, ReportSchedule
from core.permissions import Role

# Roles that see the whole library regardless of allowed_roles.
_FULL_ROLES = {Role.DIRECTOR}


def _visible_report_keys(*, user, roles: set[str]) -> set[str] | None:
    """Report keys the caller may see, or None for 'all' (director/superuser)."""
    if getattr(user, "is_superuser", False) or (roles & _FULL_ROLES):
        return None
    keys: set[str] = set()
    for report in Report.objects.all():
        allowed = set(report.allowed_roles or [])
        if roles & allowed:
            keys.add(report.key)
    return keys


def scoped_reports(*, user, roles: set[str]) -> QuerySet[Report]:
    """The library entries visible to the caller (filtered by allowed_roles)."""
    qs = Report.objects.all()
    keys = _visible_report_keys(user=user, roles=roles)
    if keys is None:
        return qs
    return qs.filter(key__in=keys)


def scoped_runs(*, user, roles: set[str]) -> QuerySet[ReportRun]:
    """Runs the caller may see: their own runs, plus any run of a report their
    roles can access. Director/superuser see all."""
    qs = ReportRun.objects.select_related("report", "requested_by").all()
    keys = _visible_report_keys(user=user, roles=roles)
    if keys is None:
        return qs
    from django.db.models import Q

    return qs.filter(Q(report__key__in=keys) | Q(requested_by=user)).distinct()


def scoped_schedules(*, user, roles: set[str]) -> QuerySet[ReportSchedule]:
    qs = ReportSchedule.objects.select_related("report", "created_by").all()
    keys = _visible_report_keys(user=user, roles=roles)
    if keys is None:
        return qs
    return qs.filter(report__key__in=keys)
