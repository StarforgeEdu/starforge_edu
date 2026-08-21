"""Reports write-side services (D4-LB-4/5/6).

Creating a ``ReportRun`` enqueues ``build_report`` on commit; the task renders the
generator output, uploads to S3, and delivers a signed URL through
``notifications.dispatch`` (never the email client directly — DoD/§docs). The
hourly schedule scan (``run_due_report_schedules``) fires due ``ReportSchedule``
rows, guarded by ``last_run_at`` so re-running within the cadence window is a
no-op.
"""

from __future__ import annotations

import calendar
import json
import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.reports.authorization import (
    MAX_SCOPE_ITEMS,
    ReportScopeSnapshot,
    can_access_report,
    compatible_membership_scopes,
    department_key,
    has_organization_scope,
    public_params,
    snapshot_from_params,
    teacher_key,
)
from apps.reports.generators import get_generator
from apps.reports.models import Report, ReportFormat, ReportRun, ReportSchedule
from core.exceptions import ConflictException, PermissionException, ThrottledException, ValidationException
from core.job_limits import lock_tenant_job_queue, release_job_execution, try_acquire_job_execution
from core.permissions import MembershipGrantScope, PermissionRoleSet, Role
from core.tenant_context import assert_tenant_context
from core.utils import current_schema

logger = logging.getLogger("starforge.reports")

# The dispatch event name carried to the in-app/WS channel (Lane C consumes it).
REPORT_READY_EVENT = "report.ready"

_PARAMS_BY_REPORT: dict[str, set[str]] = {
    "enrollment": {"branch_id", "cohort_id"},
    "attendance": {"branch_id", "cohort_id", "date_from", "date_to"},
    "grades": {"branch_id", "term_id", "subject_id", "include_unpublished"},
    "finance": {"branch_id", "date_from", "date_to"},
    "ai_usage": {"month"},
    "storage_usage": set(),
}


def _positive_int(params: dict[str, Any], name: str) -> int | None:
    value = params.get(name)
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not (
        isinstance(value, int) or (isinstance(value, str) and value.isdecimal())
    ):
        raise ValidationException(code="invalid_report_params", fields={name: ["Must be an integer."]})
    value = int(value)
    if value < 1:
        raise ValidationException(code="invalid_report_params", fields={name: ["Must be positive."]})
    return value


def _validate_date_param(params: dict[str, Any], name: str) -> date | None:
    value = params.get(name)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValidationException(code="invalid_report_params", fields={name: ["Use YYYY-MM-DD."]})
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationException(
            code="invalid_report_params", fields={name: ["Use a valid YYYY-MM-DD date."]}
        ) from exc
    params[name] = parsed.isoformat()
    return parsed


def _live_roles_for_users(users: list[Any]) -> dict[int, PermissionRoleSet]:
    """Resolve canonical roles for many worker/recipient principals in 2 queries.

    This mirrors ``core.permissions.get_user_roles`` without a synthetic request
    and avoids a two-query N+1 when a schedule carries up to 50 recipients.
    """
    active_users = {
        user.pk: user for user in users if user is not None and user.pk and getattr(user, "is_active", False)
    }
    if not active_users:
        return {}

    from apps.access.models import AccountTypePermission
    from apps.users.models import RoleMembership

    memberships = list(
        RoleMembership.objects.filter(user_id__in=active_users, revoked_at__isnull=True)
        .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        .select_related("account_type")
    )
    account_type_ids = {
        membership.account_type_id for membership in memberships if membership.account_type_id is not None
    }
    grants_by_type: dict[int, set[str]] = {account_type_id: set() for account_type_id in account_type_ids}
    for account_type_id, permission in AccountTypePermission.objects.filter(
        account_type_id__in=account_type_ids
    ).values_list("account_type_id", "permission"):
        grants_by_type[account_type_id].add(permission)

    by_user: dict[int, list[Any]] = {user_id: [] for user_id in active_users}
    for membership in memberships:
        by_user[membership.user_id].append(membership)

    legacy_kind_by_role = {
        Role.TEACHER: "teacher",
        Role.STUDENT: "student",
        Role.PARENT: "parent",
    }
    resolved: dict[int, PermissionRoleSet] = {}
    for user_id, user_memberships in by_user.items():
        scopes: list[MembershipGrantScope] = []
        canonical_grants: set[str] = set()
        fallback_roles: set[str] = set()
        for membership in user_memberships:
            if membership.account_type_id is None:
                role = membership.role
                account_kind = legacy_kind_by_role.get(role, "staff")
                grants: set[str] = set()
                fallback_roles.add(role)
                is_legacy = True
                is_organization_wide = role == Role.DIRECTOR
            else:
                role = membership.account_type.compatibility_role
                account_kind = membership.account_type.account_kind
                grants = grants_by_type.get(membership.account_type_id, set())
                canonical_grants.update(grants)
                is_legacy = False
                is_organization_wide = membership.account_type.is_owner_type
            scopes.append(
                MembershipGrantScope(
                    branch_id=membership.branch_id,
                    department_id=membership.department_id,
                    role=role,
                    account_kind=account_kind,
                    grants=frozenset(grants),
                    is_legacy_fallback=is_legacy,
                    is_organization_wide=is_organization_wide,
                )
            )
        resolved[user_id] = PermissionRoleSet(
            (scope.role for scope in scopes),
            canonical_grants=canonical_grants,
            fallback_roles=fallback_roles,
            account_kinds=(scope.account_kind for scope in scopes),
            membership_scopes=scopes,
        )
    return resolved


def _live_roles(user) -> PermissionRoleSet:
    """Resolve canonical grants/scopes fresh for a non-request execution path."""
    if user is None:
        return PermissionRoleSet()
    return _live_roles_for_users([user]).get(user.pk, PermissionRoleSet())


def _taught_cohorts_for_users(
    user_ids: set[int],
) -> dict[int, dict[int, tuple[int, int | None]]]:
    """Batch natural teacher scope without a recipient-count query fan-out."""
    cohorts: dict[int, dict[int, tuple[int, int | None]]] = {user_id: {} for user_id in user_ids}
    if not user_ids:
        return cohorts

    from apps.cohorts.models import Cohort, CohortTeacher
    from apps.schedule.models import Lesson

    sources = (
        Cohort.objects.filter(primary_teacher__user_id__in=user_ids).values_list(
            "primary_teacher__user_id", "id", "branch_id", "department_id"
        ),
        CohortTeacher.objects.filter(teacher__user_id__in=user_ids).values_list(
            "teacher__user_id", "cohort_id", "cohort__branch_id", "cohort__department_id"
        ),
        Lesson.objects.filter(teacher__user_id__in=user_ids).values_list(
            "teacher__user_id", "cohort_id", "cohort__branch_id", "cohort__department_id"
        ),
    )
    for rows in sources:
        for user_id, cohort_id, branch_id, department_id in rows:
            cohorts[user_id][cohort_id] = (branch_id, department_id)
    return cohorts


def _legacy_scope_rows(*, user, roles: set[str]) -> list[tuple[str, int, int | None]]:
    """Compatibility-only boundaries for old direct service callers."""
    if user is None:
        return []
    rows = (
        user.role_memberships.filter(revoked_at__isnull=True, role__in=set(roles))
        .filter(Q(account_type__isnull=True) | Q(account_type__is_active=True))
        .values_list("role", "branch_id", "department_id")
    )
    return [
        ("teacher" if role == Role.TEACHER else "staff", branch_id, department_id)
        for role, branch_id, department_id in rows
    ]


def _build_scope_snapshot(
    *,
    report_key: str,
    user,
    roles: set[str],
    target_branch: int | None,
    target_cohort_id: int | None,
    target_cohort_department_id: int | None,
) -> ReportScopeSnapshot:
    """Derive a deterministic scope from exact compatible live memberships."""
    is_superuser = bool(getattr(user, "is_superuser", False))
    if has_organization_scope(
        roles=roles,
        report_key=report_key,
        report_permission="reports:write",
        is_superuser=is_superuser,
    ):
        # A requested branch/cohort narrows an organization-wide report and is
        # intentionally shareable with an authorized branch-wide reader.
        if target_branch is not None:
            return ReportScopeSnapshot(branch_ids=(target_branch,))
        return ReportScopeSnapshot(organization=True)

    if isinstance(roles, PermissionRoleSet):
        raw_scopes = [
            (membership.account_kind, membership.branch_id, membership.department_id)
            for membership in compatible_membership_scopes(
                roles=roles,
                report_key=report_key,
                report_permission="reports:write",
            )
            if not membership.is_organization_wide
        ]
    else:
        raw_scopes = _legacy_scope_rows(user=user, roles=roles)

    taught_boundaries: set[tuple[int, int, int | None]] = set()
    if any(account_kind == "teacher" for account_kind, _branch, _department in raw_scopes):
        from apps.cohorts.selectors import taught_cohorts

        taught_boundaries = set(
            taught_cohorts(user=user).values_list("id", "branch_id", "department_id").distinct()
        )

    branch_ids: set[int] = set()
    department_keys: set[str] = set()
    teacher_cohorts_by_key: dict[str, set[int]] = {}
    for account_kind, branch_id, department_id in raw_scopes:
        if target_branch is not None and branch_id != target_branch:
            continue
        if (
            target_cohort_id is not None
            and department_id is not None
            and department_id != target_cohort_department_id
        ):
            continue
        if account_kind == "staff":
            if department_id is None:
                branch_ids.add(branch_id)
            else:
                department_keys.add(department_key(branch_id, department_id))
            continue
        if account_kind != "teacher" or user is None:
            continue
        visible_taught = {
            cohort_id
            for cohort_id, cohort_branch_id, cohort_department_id in taught_boundaries
            if cohort_branch_id == branch_id
            and (department_id is None or cohort_department_id == department_id)
        }
        if target_cohort_id is not None:
            if target_cohort_id not in visible_taught:
                continue
        elif not visible_taught:
            continue
        scope_key = teacher_key(user.pk, branch_id, department_id)
        teacher_cohorts_by_key[scope_key] = (
            {target_cohort_id} if target_cohort_id is not None else visible_taught
        )

    # Collapse narrower scopes already covered by a compatible staff grant.
    department_keys = {key for key in department_keys if int(key.partition(":")[0]) not in branch_ids}
    covered_departments = set(department_keys)
    teacher_cohorts_by_key = {
        key: cohort_ids
        for key, cohort_ids in teacher_cohorts_by_key.items()
        if int(key.split(":", 2)[1]) not in branch_ids
        and (
            int(key.rpartition(":")[2]) == 0
            or department_key(int(key.split(":", 2)[1]), int(key.rpartition(":")[2]))
            not in covered_departments
        )
    }
    snapshot = ReportScopeSnapshot(
        branch_ids=tuple(sorted(branch_ids)),
        department_keys=tuple(sorted(department_keys)),
        teacher_keys=tuple(sorted(teacher_cohorts_by_key)),
        teacher_cohort_ids=tuple(
            sorted({cohort_id for cohort_ids in teacher_cohorts_by_key.values() for cohort_id in cohort_ids})
        ),
    )
    if snapshot.is_empty:
        raise PermissionException(code="report_forbidden")
    if any(
        len(values) > MAX_SCOPE_ITEMS
        for values in (
            snapshot.branch_ids,
            snapshot.department_keys,
            snapshot.teacher_keys,
            snapshot.teacher_cohort_ids,
        )
    ):
        raise ValidationException(code="report_scope_too_large")
    return snapshot


def _normalize_params(*, report_key: str, params: dict[str, Any], user, roles: set[str]) -> dict[str, Any]:
    """Validate report inputs and stamp an unforgeable exact-scope snapshot."""
    if user is None or not getattr(user, "is_active", False):
        raise PermissionException(code="report_forbidden")
    if not isinstance(params, dict):
        raise ValidationException(code="invalid_report_params", fields={"params": ["Must be an object."]})
    params = public_params(params)
    if len(params) > 20 or len(json.dumps(params, default=str)) > 16_384:
        raise ValidationException(code="invalid_report_params", fields={"params": ["Payload is too large."]})
    allowed = _PARAMS_BY_REPORT.get(report_key, set())
    # Never trust persisted/client-provided scope metadata: discard every
    # server-owned key and recompute it from current memberships below.
    unknown = set(params) - allowed
    if unknown:
        raise ValidationException(
            code="invalid_report_params",
            fields={"params": [f"Unknown parameter(s): {', '.join(sorted(unknown))}."]},
        )

    clean = dict(params)
    branch_id = _positive_int(clean, "branch_id")
    cohort_id = _positive_int(clean, "cohort_id")
    for name in ("term_id", "subject_id"):
        if name in clean:
            parsed = _positive_int(clean, name)
            if parsed is not None:
                clean[name] = parsed
    if branch_id is not None:
        clean["branch_id"] = branch_id
    if cohort_id is not None:
        clean["cohort_id"] = cohort_id

    start = _validate_date_param(clean, "date_from")
    end = _validate_date_param(clean, "date_to")
    if start and end and start > end:
        raise ValidationException(
            code="invalid_report_params", fields={"date_to": ["Must not be before date_from."]}
        )
    if "include_unpublished" in clean and not isinstance(clean["include_unpublished"], bool):
        raise ValidationException(
            code="invalid_report_params", fields={"include_unpublished": ["Must be a boolean."]}
        )
    if clean.get("include_unpublished") and not has_organization_scope(
        roles=roles,
        report_key=report_key,
        report_permission="reports:write",
        is_superuser=bool(getattr(user, "is_superuser", False)),
    ):
        raise PermissionException(code="report_forbidden")
    month = clean.get("month")
    if month not in (None, ""):
        if not isinstance(month, str) or len(month) != 7:
            raise ValidationException(code="invalid_report_params", fields={"month": ["Use YYYY-MM."]})
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise ValidationException(
                code="invalid_report_params", fields={"month": ["Use a valid YYYY-MM."]}
            ) from exc

    from apps.org.models import Branch

    target_branch: int | None = None
    target_cohort_department_id: int | None = None
    if branch_id is not None:
        if not Branch.objects.filter(pk=branch_id, archived_at__isnull=True).exists():
            raise ValidationException(code="invalid_report_params", fields={"branch_id": ["Not found."]})
        target_branch = branch_id
    if cohort_id is not None:
        from apps.cohorts.models import Cohort

        cohort_scope = (
            Cohort.objects.filter(pk=cohort_id, is_archived=False)
            .values_list("branch_id", "department_id")
            .first()
        )
        if cohort_scope is None:
            raise ValidationException(code="invalid_report_params", fields={"cohort_id": ["Not found."]})
        cohort_branch, target_cohort_department_id = cohort_scope
        if target_branch is not None and target_branch != cohort_branch:
            raise ValidationException(
                code="invalid_report_params",
                fields={"cohort_id": ["The cohort is not in the selected branch."]},
            )
        target_branch = cohort_branch

    snapshot = _build_scope_snapshot(
        report_key=report_key,
        user=user,
        roles=roles,
        target_branch=target_branch,
        target_cohort_id=cohort_id,
        target_cohort_department_id=target_cohort_department_id,
    )
    clean.update(snapshot.as_params())
    return clean


def _principal_covers_scope(
    *,
    user,
    report: Report,
    snapshot: ReportScopeSnapshot,
    roles: PermissionRoleSet | None = None,
    report_permission: str = "reports:read",
    taught_cohorts: dict[int, tuple[int, int | None]] | None = None,
    scope_cohorts: Mapping[int, tuple[int, int | None]] | None = None,
) -> bool:
    roles = roles if roles is not None else _live_roles(user)
    if not can_access_report(
        report=report,
        roles=roles,
        report_permission=report_permission,
        is_superuser=bool(getattr(user, "is_superuser", False)),
    ):
        return False
    if has_organization_scope(
        roles=roles,
        report_key=report.key,
        report_permission=report_permission,
        is_superuser=bool(getattr(user, "is_superuser", False)),
    ):
        return True
    if snapshot.organization:
        return False

    branch_wide: set[int] = set()
    departments: set[str] = set()
    teacher_scopes: set[str] = set()
    teacher_memberships = []
    for membership in compatible_membership_scopes(
        roles=roles,
        report_key=report.key,
        report_permission=report_permission,
    ):
        if membership.is_organization_wide:
            return True
        if membership.account_kind == "staff":
            if membership.department_id is None:
                branch_wide.add(membership.branch_id)
            else:
                departments.add(department_key(membership.branch_id, membership.department_id))
        elif membership.account_kind == "teacher":
            teacher_memberships.append(membership)

    if teacher_memberships:
        if taught_cohorts is None:
            taught_cohorts = _taught_cohorts_for_users({user.pk})[user.pk]
        taught_boundaries = set(taught_cohorts.values())
        for membership in teacher_memberships:
            if any(
                branch_id == membership.branch_id
                and (membership.department_id is None or department_id == membership.department_id)
                for branch_id, department_id in taught_boundaries
            ):
                teacher_scopes.add(teacher_key(user.pk, membership.branch_id, membership.department_id))

    if not set(snapshot.branch_ids).issubset(branch_wide):
        return False
    for key in snapshot.department_keys:
        branch_id = int(key.partition(":")[0])
        if branch_id not in branch_wide and key not in departments:
            return False
    for key in snapshot.teacher_keys:
        _owner_id, branch_id_raw, department_id_raw = key.split(":", 2)
        branch_id = int(branch_id_raw)
        department_id = int(department_id_raw)
        if branch_id in branch_wide:
            continue
        if department_id and department_key(branch_id, department_id) in departments:
            continue
        if key not in teacher_scopes:
            return False
    if snapshot.teacher_cohort_ids:
        if scope_cohorts is None:
            from apps.cohorts.models import Cohort

            scope_cohorts = {
                cohort_id: (branch_id, department_id)
                for cohort_id, branch_id, department_id in Cohort.objects.filter(
                    pk__in=snapshot.teacher_cohort_ids
                ).values_list("id", "branch_id", "department_id")
            }
        if set(scope_cohorts) != set(snapshot.teacher_cohort_ids):
            return False
        taught_ids = set(taught_cohorts or {})
        for cohort_id, (branch_id, cohort_department_id) in scope_cohorts.items():
            if branch_id in branch_wide:
                continue
            if (
                cohort_department_id is not None
                and department_key(branch_id, cohort_department_id) in departments
            ):
                continue
            if cohort_id not in taught_ids:
                return False
    return True


def _validate_recipient_ids(*, recipient_ids: Any, report: Report, params: dict[str, Any]) -> list[int]:
    if not isinstance(recipient_ids, list) or len(recipient_ids) > 50:
        raise ValidationException(
            code="invalid_recipients", fields={"recipient_ids": ["Must contain at most 50 user ids."]}
        )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in recipient_ids):
        raise ValidationException(
            code="invalid_recipients", fields={"recipient_ids": ["Every id must be a positive integer."]}
        )
    unique = list(dict.fromkeys(recipient_ids))
    if not unique:
        return []
    from apps.users.models import User

    snapshot = snapshot_from_params(params)
    if snapshot is None:
        raise ValidationException(code="invalid_report_scope")
    from apps.cohorts.models import Cohort

    scope_cohorts = {
        cohort_id: (branch_id, department_id)
        for cohort_id, branch_id, department_id in Cohort.objects.filter(
            pk__in=snapshot.teacher_cohort_ids
        ).values_list("id", "branch_id", "department_id")
    }
    if set(scope_cohorts) != set(snapshot.teacher_cohort_ids):
        raise ValidationException(code="invalid_report_scope")
    users = {user.pk: user for user in User.objects.filter(pk__in=unique, is_active=True)}
    roles_by_user = _live_roles_for_users(list(users.values()))
    taught_by_user = _taught_cohorts_for_users(
        {user_id for user_id, roles in roles_by_user.items() if "teacher" in roles.account_kinds}
    )
    if set(users) != set(unique) or any(
        not _principal_covers_scope(
            user=users[user_id],
            report=report,
            snapshot=snapshot,
            roles=roles_by_user.get(user_id, PermissionRoleSet()),
            taught_cohorts=taught_by_user.get(user_id, {}),
            scope_cohorts=scope_cohorts,
        )
        for user_id in unique
    ):
        raise ValidationException(
            code="invalid_recipients",
            fields={
                "recipient_ids": [
                    "A recipient is inactive, missing, unauthorized, or outside the exact report scope."
                ]
            },
        )
    return unique


def can_run_report(*, report: Report, roles: set[str], is_superuser: bool = False) -> bool:
    """Compound reports:write + source-domain gate for one report."""
    return can_access_report(
        report=report,
        roles=roles,
        report_permission="reports:write",
        is_superuser=is_superuser,
    )


def _admit_report_run(
    *, report: Report, requested_by, params: dict[str, Any], fmt: str, recipient_ids: list[int] | None = None
) -> tuple[ReportRun, bool]:
    """Return an identical active run or create one after concurrency-safe caps."""
    lock_tenant_job_queue("documents")
    recipient_ids = recipient_ids or []
    active = (ReportRun.Status.QUEUED, ReportRun.Status.RUNNING)
    duplicate = (
        ReportRun.objects.filter(
            report=report,
            requested_by=requested_by,
            params=params,
            format=fmt,
            recipient_ids=recipient_ids,
            status__in=active,
        )
        .order_by("-created_at")
        .first()
    )
    if duplicate is not None:
        return duplicate, False

    now = timezone.now()
    user_active = ReportRun.objects.filter(requested_by=requested_by, status__in=active).count()
    tenant_active = ReportRun.objects.filter(status__in=active).count()
    user_hourly = ReportRun.objects.filter(
        requested_by=requested_by, created_at__gte=now - timedelta(hours=1)
    ).count()
    tenant_hourly = ReportRun.objects.filter(created_at__gte=now - timedelta(hours=1)).count()
    from apps.academics.models import Transcript

    transcript_active = Transcript.objects.filter(
        status__in=(Transcript.Status.PENDING, Transcript.Status.PROCESSING)
    ).count()
    transcript_hourly = Transcript.objects.filter(created_at__gte=now - timedelta(hours=1)).count()
    if user_active >= getattr(settings, "REPORT_MAX_ACTIVE_PER_USER", 3):
        raise ThrottledException(code="report_user_queue_full", wait=60)
    if tenant_active >= getattr(settings, "REPORT_MAX_ACTIVE_PER_TENANT", 20):
        raise ThrottledException(code="report_tenant_queue_full", wait=60)
    if tenant_active + transcript_active >= getattr(settings, "DOCUMENT_MAX_ACTIVE_PER_TENANT", 20):
        raise ThrottledException(code="document_tenant_queue_full", wait=60)
    if user_hourly >= getattr(settings, "REPORT_MAX_HOURLY_PER_USER", 10):
        raise ThrottledException(code="report_user_hourly_limit", wait=3600)
    if tenant_hourly >= getattr(settings, "REPORT_MAX_HOURLY_PER_TENANT", 100):
        raise ThrottledException(code="report_tenant_hourly_limit", wait=3600)
    if tenant_hourly + transcript_hourly >= getattr(settings, "DOCUMENT_MAX_HOURLY_PER_TENANT", 100):
        raise ThrottledException(code="document_tenant_hourly_limit", wait=3600)

    return (
        ReportRun.objects.create(
            report=report,
            requested_by=requested_by,
            params=params,
            recipient_ids=recipient_ids,
            format=fmt,
            status=ReportRun.Status.QUEUED,
        ),
        True,
    )


@transaction.atomic
def create_report_run(
    *,
    report_key: str,
    fmt: str | None,
    params: dict[str, Any],
    requested_by,
    roles: set[str],
    recipient_ids: list[int] | None = None,
) -> ReportRun:
    """Validate the key/format/visibility, create a queued ReportRun, enqueue
    ``build_report`` after commit. Raises 403 when the caller's roles are not in
    the report's allowed_roles, 422 for an unknown key/format."""
    get_generator(report_key)  # 422 unknown_report_key
    if requested_by is None or not getattr(requested_by, "is_active", False):
        raise PermissionException(code="report_forbidden")
    try:
        report = Report.objects.get(key=report_key)
    except Report.DoesNotExist as exc:
        raise ValidationException(code="unknown_report_key") from exc

    is_superuser = bool(getattr(requested_by, "is_superuser", False))
    if not can_run_report(report=report, roles=roles, is_superuser=is_superuser):
        raise PermissionException(code="report_forbidden")

    chosen = fmt or report.default_format
    if chosen not in ReportFormat.values:
        raise ValidationException(code="invalid_format")

    normalized_params = _normalize_params(
        report_key=report.key,
        params=params or {},
        user=requested_by,
        roles=roles,
    )
    recipients = _validate_recipient_ids(
        recipient_ids=recipient_ids or [],
        report=report,
        params=normalized_params,
    )
    run, created = _admit_report_run(
        report=report,
        requested_by=requested_by,
        params=normalized_params,
        fmt=chosen,
        recipient_ids=recipients,
    )
    schema = current_schema()
    run_id = run.pk
    if created:
        transaction.on_commit(lambda: _enqueue_build(run_id, schema))
    return run


def _enqueue_build(run_id: int, schema: str) -> None:
    from celery_tasks.report_tasks import build_report

    build_report.delay(run_id, _schema_name=schema)


# --------------------------------------------------------------------------- #
# build_report body (called by the Celery task — D4-LB-4)
# --------------------------------------------------------------------------- #
def build_report_run(run_id: int) -> str | None:
    if not try_acquire_job_execution("report", run_id):
        raise ConflictException(_("This report run is already being built."), code="report_in_progress")
    try:
        return _build_report_run(run_id)
    finally:
        release_job_execution("report", run_id)


def _build_report_run(run_id: int) -> str | None:
    """Idempotent task body: queued → running → done | failed.

    Renders the generator output for the run's scoping (the requester's roles),
    uploads to ``{schema}/reports/{run_id}.{ext}``, presigns a download URL, and
    dispatches a ``report.ready`` notification to the requester. A run not in
    ``queued`` is skipped (safe re-delivery). Returns the s3 key, or None when
    skipped.
    """
    from infrastructure.storage import s3_client

    run = ReportRun.objects.select_related("report", "requested_by").get(pk=run_id)
    if run.format not in ReportFormat.values:
        raise ValidationException(code="invalid_format")
    if run.status not in ReportRun.Status.values:
        raise ValidationException(code="invalid_report_status")
    if run.status in (ReportRun.Status.DONE, ReportRun.Status.FAILED):
        # Terminal — a re-delivery is a no-op (idempotent).
        return run.s3_key if run.s3_key == _expected_run_key(run) else None
    # QUEUED or RUNNING: RUNNING means a prior worker was hard-killed (OOM/SIGKILL)
    # mid-render before its `except` could reset the run — build_report is acks_late,
    # so the broker redelivers the task. Re-DRIVE it (render is idempotent: it
    # overwrites the same S3 key), rather than early-returning and stranding the run
    # in RUNNING forever with no file, no notification, no failure, no retry.

    if run.requested_by is None or not run.requested_by.is_active:
        raise PermissionException(code="report_forbidden")
    roles = _live_roles(run.requested_by)
    if not can_run_report(
        report=run.report,
        roles=roles,
        is_superuser=bool(getattr(run.requested_by, "is_superuser", False)),
    ):
        raise PermissionException(code="report_forbidden")
    prior_scope = snapshot_from_params(run.params)
    normalized_params = _normalize_params(
        report_key=run.report.key,
        params=run.params or {},
        user=run.requested_by,
        roles=roles,
    )
    current_scope = snapshot_from_params(normalized_params)
    if current_scope is None or (prior_scope is not None and prior_scope != current_scope):
        # A queued report must not silently move to another branch/department
        # after revocation or assignment changes.
        raise PermissionException(code="report_scope_changed")

    run.params = normalized_params
    run.status = ReportRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["params", "status", "started_at"])

    generator = get_generator(run.report.key)
    data = generator.collect(normalized_params, user=run.requested_by, roles=roles)

    locale = _requester_locale(run.requested_by)
    payload = generator.render(data, run.format, locale=locale)

    # Rendering can be expensive. Re-resolve authorization before publication
    # so a revocation that lands while the worker renders cannot yield a
    # downloadable artifact or ready notification.
    latest_roles = _live_roles(run.requested_by)
    if not can_run_report(
        report=run.report,
        roles=latest_roles,
        is_superuser=bool(getattr(run.requested_by, "is_superuser", False)),
    ):
        raise PermissionException(code="report_forbidden")
    latest_params = _normalize_params(
        report_key=run.report.key,
        params=normalized_params,
        user=run.requested_by,
        roles=latest_roles,
    )
    if snapshot_from_params(latest_params) != current_scope:
        raise PermissionException(code="report_scope_changed")

    content_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if run.format == ReportFormat.XLSX
        else "application/pdf"
    )
    key = _expected_run_key(run)
    s3_client.upload_bytes(key, payload, content_type=content_type)

    run.s3_key = key
    run.file_bytes = len(payload)
    run.status = ReportRun.Status.DONE
    run.finished_at = timezone.now()
    run.save(update_fields=["s3_key", "file_bytes", "status", "finished_at"])

    _notify_ready(run)
    return key


def mark_run_failed(run_id: int, exc: Exception) -> None:
    logger.error(
        "Report run %s failed",
        run_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    ReportRun.objects.filter(pk=run_id).exclude(status=ReportRun.Status.DONE).update(
        status=ReportRun.Status.FAILED,
        error="report_generation_failed",
        finished_at=timezone.now(),
    )


def reset_run_for_retry(run_id: int) -> None:
    """Put a RUNNING/mid-flight run back to QUEUED so a Celery retry re-executes
    it. build_report_run early-returns on any non-QUEUED status and flips the run
    to RUNNING before doing work, so without this reset every retry would
    short-circuit and the retry budget was a dead no-op."""
    ReportRun.objects.filter(pk=run_id).exclude(status=ReportRun.Status.DONE).update(
        status=ReportRun.Status.QUEUED,
        started_at=None,
    )


def _requester_locale(user) -> str:
    return getattr(user, "preferred_language", "") or "uz"


def _expected_run_key(run: ReportRun) -> str:
    assert_tenant_context()
    if run.format not in ReportFormat.values:
        return ""
    ext = "xlsx" if run.format == ReportFormat.XLSX else "pdf"
    return f"{current_schema()}/reports/{run.pk}.{ext}"


def presign_run(run: ReportRun) -> str | None:
    """Presign only the exact server-derived key for this tenant/run/format."""
    expected_key = _expected_run_key(run)
    if run.status == ReportRun.Status.DONE and run.s3_key == expected_key:
        from infrastructure.storage import s3_client

        return s3_client.presign_download(expected_key, expires_in=600)
    return None


def _notify_ready(run: ReportRun) -> None:
    """Deliver the ready signed URL via notifications.dispatch (never email
    directly). Recipients are the requester PLUS any ``recipient_ids`` configured
    on the originating schedule (deduped); a fresh presign is embedded so the
    in-app/WS payload carries a working link."""
    snapshot = snapshot_from_params(run.params)
    download_url = presign_run(run)
    if snapshot is None or download_url is None:
        return
    # Preserve order (requester first), dedupe, then re-authorize every recipient
    # immediately before disclosing a bearer download URL.
    requested_ids = list(
        dict.fromkeys(rid for rid in [run.requested_by_id, *(run.recipient_ids or [])] if rid)
    )
    if not requested_ids:
        return
    from apps.users.models import User

    candidates = {user.pk: user for user in User.objects.filter(pk__in=requested_ids, is_active=True)}
    from apps.cohorts.models import Cohort

    scope_cohorts = {
        cohort_id: (branch_id, department_id)
        for cohort_id, branch_id, department_id in Cohort.objects.filter(
            pk__in=snapshot.teacher_cohort_ids
        ).values_list("id", "branch_id", "department_id")
    }
    if set(scope_cohorts) != set(snapshot.teacher_cohort_ids):
        return
    roles_by_user = _live_roles_for_users(list(candidates.values()))
    taught_by_user = _taught_cohorts_for_users(
        {user_id for user_id, roles in roles_by_user.items() if "teacher" in roles.account_kinds}
    )
    recipients = [
        user_id
        for user_id in requested_ids
        if user_id in candidates
        and _principal_covers_scope(
            user=candidates[user_id],
            report=run.report,
            snapshot=snapshot,
            roles=roles_by_user.get(user_id, PermissionRoleSet()),
            taught_cohorts=taught_by_user.get(user_id, {}),
            scope_cohorts=scope_cohorts,
        )
    ]
    if not recipients:
        return
    from apps.notifications.services import dispatch

    schema = current_schema()
    context = {
        "report": run.report.key,
        "report_title": run.report.title,
        "run_id": run.pk,
        "format": run.format,
        "download_url": download_url,
    }
    for rid in recipients:
        dispatch(
            event_type=REPORT_READY_EVENT,
            recipient_id=rid,
            context=context,
            # Per-recipient key so the in-app dedupe doesn't collapse deliveries.
            dedupe_key=f"report.ready:{schema}:{run.pk}:{rid}",
        )


# --------------------------------------------------------------------------- #
# Schedules (D4-LB-6)
# --------------------------------------------------------------------------- #
@transaction.atomic
def create_schedule(*, report_key: str, created_by, roles: set[str], **fields: Any) -> ReportSchedule:
    if created_by is None or not getattr(created_by, "is_active", False):
        raise PermissionException(code="report_forbidden")
    try:
        report = Report.objects.get(key=report_key)
    except Report.DoesNotExist as exc:
        raise ValidationException(code="unknown_report_key") from exc
    is_superuser = bool(getattr(created_by, "is_superuser", False))
    if not can_run_report(report=report, roles=roles, is_superuser=is_superuser):
        raise PermissionException(code="report_forbidden")
    fields = dict(fields)
    if fields.get("format", ReportFormat.PDF) not in ReportFormat.values:
        raise ValidationException(code="invalid_format")
    fields["params"] = _normalize_params(
        report_key=report.key,
        params=fields.get("params") or {},
        user=created_by,
        roles=roles,
    )
    fields["recipient_ids"] = _validate_recipient_ids(
        recipient_ids=fields.get("recipient_ids") or [],
        report=report,
        params=fields["params"],
    )
    return ReportSchedule.objects.create(report=report, created_by=created_by, **fields)


@transaction.atomic
def update_schedule(
    schedule: ReportSchedule, *, actor, roles: set[str], report_key: str | None = None, **changes: Any
) -> ReportSchedule:
    """Update an already selector-scoped schedule with the same create gates."""
    if actor is None or not getattr(actor, "is_active", False):
        raise PermissionException(code="report_forbidden")
    report = schedule.report
    existing_scope = snapshot_from_params(schedule.params)
    current_roles = _live_roles(actor)
    if existing_scope is None:
        # Only a current organization-wide writer may adopt a legacy schedule
        # whose persisted department/teacher scope cannot be proven.
        if not has_organization_scope(
            roles=current_roles,
            report_key=report.key,
            report_permission="reports:write",
            is_superuser=bool(getattr(actor, "is_superuser", False)),
        ):
            raise PermissionException(code="report_forbidden")
    else:
        from apps.cohorts.models import Cohort

        existing_cohorts = {
            cohort_id: (branch_id, department_id)
            for cohort_id, branch_id, department_id in Cohort.objects.filter(
                pk__in=existing_scope.teacher_cohort_ids
            ).values_list("id", "branch_id", "department_id")
        }
        if not _principal_covers_scope(
            user=actor,
            report=report,
            snapshot=existing_scope,
            roles=current_roles,
            report_permission="reports:write",
            scope_cohorts=existing_cohorts,
        ):
            raise PermissionException(code="report_forbidden")
    if report_key is not None:
        try:
            report = Report.objects.get(key=report_key)
        except Report.DoesNotExist as exc:
            raise ValidationException(code="unknown_report_key") from exc
    if not can_run_report(
        report=report,
        roles=current_roles,
        is_superuser=bool(getattr(actor, "is_superuser", False)),
    ):
        raise PermissionException(code="report_forbidden")
    if changes.get("format", schedule.format) not in ReportFormat.values:
        raise ValidationException(code="invalid_format")

    merged_params = changes.get("params", schedule.params or {})
    normalized = _normalize_params(
        report_key=report.key,
        params=merged_params,
        user=actor,
        roles=current_roles,
    )
    recipients = _validate_recipient_ids(
        recipient_ids=changes.get("recipient_ids", schedule.recipient_ids or []),
        report=report,
        params=normalized,
    )
    schedule.report = report
    schedule.params = normalized
    schedule.recipient_ids = recipients
    for field in ("cadence", "weekday", "day_of_month", "hour", "format", "is_active"):
        if field in changes:
            setattr(schedule, field, changes[field])
    schedule.full_clean()
    schedule.save()
    return schedule


def schedule_is_due(schedule: ReportSchedule, *, now: datetime) -> bool:
    """True when ``schedule`` should fire at ``now``: cadence anchor matches the
    current weekday/day-of-month + hour, and it hasn't already run this window.

    The ``last_run_at`` guard rejects a second fire within the same cadence period
    (a re-run of the hourly scan creates no duplicate run)."""
    if not schedule.is_active:
        return False
    local = timezone.localtime(now)
    if local.hour != schedule.hour:
        return False
    if schedule.cadence == ReportSchedule.Cadence.WEEKLY:
        if local.weekday() != schedule.weekday:
            return False
        window = timedelta(days=7)
    elif schedule.cadence == ReportSchedule.Cadence.MONTHLY:
        # Clamp the anchor to the month's last day so day_of_month in {29,30,31}
        # still fires in shorter months (Feb/Apr/Jun/Sep/Nov) instead of being
        # silently skipped. e.g. "the 31st" fires on Feb 28/29.
        last_day = calendar.monthrange(local.year, local.month)[1]
        target_day = min(schedule.day_of_month or 1, last_day)
        if local.day != target_day:
            return False
        window = timedelta(days=28)
    else:  # pragma: no cover - constrained by the model
        return False
    # last_run_at guard: reject a second fire within the same cadence window
    # (a re-run of the hourly scan must create no duplicate run).
    return not (schedule.last_run_at is not None and now - schedule.last_run_at < window)


@transaction.atomic
def fire_schedule(schedule: ReportSchedule, *, now: datetime) -> ReportRun:
    """Create a queued ReportRun for a due schedule and stamp ``last_run_at``.

    Locks the schedule row so two concurrent scans can't both fire it; re-checks
    due-ness under the lock (the last_run_at guard) before creating the run.
    """
    locked = ReportSchedule.objects.select_for_update().get(pk=schedule.pk)
    if not schedule_is_due(locked, now=now):
        # Lost the race — another scan already fired it.
        raise ValidationException(code="schedule_not_due")
    creator = locked.created_by if locked.created_by_id is not None else None
    if creator is None or not creator.is_active:
        # The creator was deleted (SET_NULL). Without a requester there is no role
        # scope to generate against, so the run would be empty AND undelivered.
        # Refuse to create it here (deactivation is done by run_due_schedules,
        # OUTSIDE this atomic block — deactivating here would be rolled back by the
        # raise below).
        raise ValidationException(code="schedule_no_creator")
    if locked.format not in ReportFormat.values:
        raise ValidationException(code="invalid_format")
    creator_roles = _live_roles(creator)
    if not can_run_report(
        report=locked.report,
        roles=creator_roles,
        is_superuser=bool(getattr(creator, "is_superuser", False)),
    ):
        raise PermissionException(code="report_forbidden")
    prior_scope = snapshot_from_params(locked.params)
    normalized_params = _normalize_params(
        report_key=locked.report.key,
        params=locked.params or {},
        user=creator,
        roles=creator_roles,
    )
    current_scope = snapshot_from_params(normalized_params)
    if current_scope is None or (prior_scope is not None and prior_scope != current_scope):
        raise PermissionException(code="report_scope_changed")
    recipient_ids = _validate_recipient_ids(
        recipient_ids=list(locked.recipient_ids or []),
        report=locked.report,
        params=normalized_params,
    )
    run, created = _admit_report_run(
        report=locked.report,
        requested_by=creator,
        params=normalized_params,
        recipient_ids=recipient_ids,
        fmt=locked.format,
    )
    locked.last_run_at = now
    locked.params = normalized_params
    locked.recipient_ids = recipient_ids
    locked.save(update_fields=["params", "recipient_ids", "last_run_at"])
    schema = current_schema()
    run_id = run.pk
    if created:
        transaction.on_commit(lambda: _enqueue_build(run_id, schema))
    return run


def run_due_schedules(*, now: datetime | None = None) -> int:
    """Scan the current tenant's active schedules and fire the due ones. Returns
    the count fired. Idempotent within a cadence window (last_run_at guard)."""
    now = now or timezone.now()
    fired = 0
    candidates = list(ReportSchedule.objects.select_related("report", "created_by").filter(is_active=True))
    for schedule in candidates:
        if not schedule_is_due(schedule, now=now):
            continue
        if schedule.created_by_id is None:
            # Creator deleted → no scope/recipient. Deactivate (committed here,
            # outside any atomic) so it stops firing empty, undelivered runs.
            ReportSchedule.objects.filter(pk=schedule.pk).update(is_active=False)
            logger.warning("report schedule %s deactivated: creator deleted", schedule.pk)
            continue
        try:
            fire_schedule(schedule, now=now)
            fired += 1
        except PermissionException:
            ReportSchedule.objects.filter(pk=schedule.pk).update(is_active=False)
            logger.warning("report schedule %s deactivated: creator no longer authorized", schedule.pk)
        except ValidationException as exc:
            if exc.code != "schedule_not_due":
                ReportSchedule.objects.filter(pk=schedule.pk).update(is_active=False)
                logger.warning("report schedule %s deactivated: invalid persisted configuration", schedule.pk)
            continue
        except ThrottledException:
            # Queue pressure is temporary. Leave last_run_at untouched so the
            # next hourly scan can try again.
            continue
    return fired
