"""Permission- and scope-bound read selectors for reports.

Library visibility requires ``reports:read`` and the report source-domain grant
on one compatible membership. Run/schedule visibility additionally proves that
the complete persisted scope snapshot is contained by the caller's *current*
live scope; matching one branch of a multi-branch report is never sufficient.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.reports.authorization import (
    SCOPE_BRANCH_IDS_PARAM,
    SCOPE_DEPARTMENT_KEYS_PARAM,
    SCOPE_ORGANIZATION_PARAM,
    SCOPE_TEACHER_COHORT_IDS_PARAM,
    SCOPE_TEACHER_KEYS_PARAM,
    SCOPE_VERSION,
    SCOPE_VERSION_PARAM,
    can_access_report,
    compatible_membership_scopes,
    department_key,
    teacher_key,
)
from apps.reports.models import Report, ReportRun, ReportSchedule
from core.permissions import PermissionRoleSet, Role


def _visible_report_keys(*, user, roles: set[str]) -> set[str]:
    """Report keys authorized by live compound read grants."""
    if user is None or not getattr(user, "is_active", False):
        return set()
    return {
        report.key
        for report in Report.objects.all()
        if can_access_report(
            report=report,
            roles=roles,
            report_permission="reports:read",
            is_superuser=bool(getattr(user, "is_active", False) and getattr(user, "is_superuser", False)),
        )
    }


def scoped_reports(*, user, roles: set[str]) -> QuerySet[Report]:
    return Report.objects.filter(key__in=_visible_report_keys(user=user, roles=roles))


def _legacy_boundary_sets(*, user, roles: set[str]) -> tuple[set[int], set[str], set[str], set[int]]:
    """Compatibility boundaries for direct selector tests using plain roles."""
    if user is None:
        return set(), set(), set(), set()
    memberships = list(
        user.role_memberships.filter(revoked_at__isnull=True, role__in=set(roles))
        .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        .values_list("role", "branch_id", "department_id")
    )
    branches: set[int] = set()
    departments: set[str] = set()
    teachers: set[str] = set()
    teacher_cohort_ids: set[int] = set()
    for role, branch_id, department_id in memberships:
        if role == Role.TEACHER:
            teachers.add(teacher_key(user.pk, branch_id, department_id))
        elif department_id is None:
            branches.add(branch_id)
        else:
            departments.add(department_key(branch_id, department_id))
    if teachers:
        from apps.cohorts.selectors import taught_cohorts

        teacher_cohort_ids = set(taught_cohorts(user=user).values_list("id", flat=True))
    return branches, departments, teachers, teacher_cohort_ids


def _current_boundary_sets(
    *, user, roles: set[str], report_key: str
) -> tuple[bool, set[int], set[str], set[str], set[int]]:
    if bool(getattr(user, "is_active", False) and getattr(user, "is_superuser", False)):
        return True, set(), set(), set(), set()
    if not isinstance(roles, PermissionRoleSet):
        if Role.DIRECTOR in set(roles):
            return True, set(), set(), set(), set()
        legacy_branches, legacy_departments, legacy_teachers, legacy_cohort_ids = _legacy_boundary_sets(
            user=user, roles=roles
        )
        return False, legacy_branches, legacy_departments, legacy_teachers, legacy_cohort_ids

    memberships = compatible_membership_scopes(
        roles=roles,
        report_key=report_key,
        report_permission="reports:read",
    )
    if any(membership.is_organization_wide for membership in memberships):
        return True, set(), set(), set(), set()

    branches: set[int] = set()
    departments: set[str] = set()
    teacher_memberships = []
    for membership in memberships:
        if membership.account_kind == "staff":
            if membership.department_id is None:
                branches.add(membership.branch_id)
            else:
                departments.add(department_key(membership.branch_id, membership.department_id))
        elif membership.account_kind == "teacher":
            teacher_memberships.append(membership)

    # A teacher token remains current only while the principal still teaches at
    # least one cohort inside that exact membership boundary.
    teachers: set[str] = set()
    teacher_cohort_ids: set[int] = set()
    if teacher_memberships:
        from apps.cohorts.selectors import taught_cohorts

        taught_rows = set(
            taught_cohorts(user=user).values_list("id", "branch_id", "department_id").distinct()
        )
        for membership in teacher_memberships:
            visible_cohorts = {
                cohort_id
                for cohort_id, branch_id, department_id in taught_rows
                if branch_id == membership.branch_id
                and (membership.department_id is None or department_id == membership.department_id)
            }
            if visible_cohorts:
                teachers.add(teacher_key(user.pk, membership.branch_id, membership.department_id))
                teacher_cohort_ids.update(visible_cohorts)
    return False, branches, departments, teachers, teacher_cohort_ids


def _scope_visibility_q(*, user, roles: set[str], report_key: str) -> Q:
    organization, branches, departments, teachers, teacher_cohort_ids = _current_boundary_sets(
        user=user, roles=roles, report_key=report_key
    )
    if organization:
        return Q(report__key=report_key)

    # A branch-wide membership covers every department snapshot in that branch.
    if branches:
        from apps.org.models import Department

        departments.update(
            department_key(branch_id, department_id)
            for branch_id, department_id in Department.objects.filter(branch_id__in=branches).values_list(
                "branch_id", "id"
            )
        )

    metadata = {
        "params__contains": {
            SCOPE_VERSION_PARAM: SCOPE_VERSION,
            SCOPE_ORGANIZATION_PARAM: False,
        },
        f"params__{SCOPE_BRANCH_IDS_PARAM}__contained_by": sorted(branches),
        f"params__{SCOPE_DEPARTMENT_KEYS_PARAM}__contained_by": sorted(departments),
        f"params__{SCOPE_TEACHER_KEYS_PARAM}__contained_by": sorted(teachers),
        f"params__{SCOPE_TEACHER_COHORT_IDS_PARAM}__contained_by": sorted(teacher_cohort_ids),
    }
    nonempty = (
        ~Q(**{f"params__{SCOPE_BRANCH_IDS_PARAM}": []})
        | ~Q(**{f"params__{SCOPE_DEPARTMENT_KEYS_PARAM}": []})
        | ~Q(**{f"params__{SCOPE_TEACHER_KEYS_PARAM}": []})
    )
    return Q(report__key=report_key, **metadata) & nonempty


def _scoped_objects(*, queryset, user, roles: set[str]):
    visible = Q(pk__in=[])
    for report_key in _visible_report_keys(user=user, roles=roles):
        visible |= _scope_visibility_q(user=user, roles=roles, report_key=report_key)
    return queryset.filter(visible).distinct()


def scoped_runs(*, user, roles: set[str]) -> QuerySet[ReportRun]:
    qs = ReportRun.objects.select_related("report", "requested_by").all()
    return _scoped_objects(queryset=qs, user=user, roles=roles)


def scoped_schedules(*, user, roles: set[str]) -> QuerySet[ReportSchedule]:
    qs = ReportSchedule.objects.select_related("report", "created_by").all()
    return _scoped_objects(queryset=qs, user=user, roles=roles)
