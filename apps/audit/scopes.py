"""Immutable audit-scope value objects and write-time attribution.

Audit ownership must be captured when an event is written.  Read paths must
never follow a resource's *current* student, cohort, branch, or department and
retroactively move history after an organizational change.  This module keeps
that policy explicit and fail-closed:

* callers may provide a validated :class:`AuditScopeSnapshot`;
* audited model receivers use a small allowlist of trustworthy, direct
  relationships;
* a small allowlist of genuinely organization-wide resource types is marked as
  such; and
* anything ambiguous stays ``unresolved`` for organization-wide review.

The scope stores integer snapshots, not foreign keys, so deleting or archiving
an organizational row cannot mutate the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from django_tenants.utils import get_public_schema_name

from core.utils import current_schema

SCOPED = "scoped"
ORGANIZATION = "organization"
UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class AuditScopeSnapshot:
    status: str
    branch_id: int | None = None
    department_id: int | None = None

    def __post_init__(self) -> None:
        if self.status not in {SCOPED, ORGANIZATION, UNRESOLVED}:
            raise ValueError("Unknown audit scope status.")
        if self.status == SCOPED:
            if not isinstance(self.branch_id, int) or isinstance(self.branch_id, bool):
                raise ValueError("A scoped audit event requires a branch id.")
            if self.branch_id < 1:
                raise ValueError("Audit branch ids must be positive.")
            if self.department_id is not None and (
                not isinstance(self.department_id, int)
                or isinstance(self.department_id, bool)
                or self.department_id < 1
            ):
                raise ValueError("Audit department ids must be positive.")
            return
        if self.branch_id is not None or self.department_id is not None:
            raise ValueError("Organization and unresolved events cannot carry branch scope.")


@dataclass(frozen=True, slots=True)
class AuditScopeResolution:
    scope: AuditScopeSnapshot
    reason: str


def scoped_audit_scope(branch_id: int, department_id: int | None = None) -> AuditScopeSnapshot:
    return AuditScopeSnapshot(
        status=SCOPED,
        branch_id=branch_id,
        department_id=department_id,
    )


def organization_audit_scope() -> AuditScopeSnapshot:
    return AuditScopeSnapshot(status=ORGANIZATION)


def unresolved_audit_scope() -> AuditScopeSnapshot:
    return AuditScopeSnapshot(status=UNRESOLVED)


# These operations genuinely concern the whole tenant rather than the actor's
# current branch.  A scoped manager must not see them just because the actor has
# a membership in the same branch.
_ORGANIZATION_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "access.AccountType",
        "access.RolePermissionOverride",
        "access.account_type",
        "access.account_type_permissions",
        "auth.OTP",
        "billing.Subscription",
        "campaign_do_not_contact",
        "org.CenterSettings",
        "payments.ProviderConfig",
        "staff_tasks.RoleGrade",
        "users.Session",
        "users.User",
    }
)


def infer_audit_scope(
    *,
    resource_type: str,
    resource_id: str | int = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditScopeSnapshot:
    """Infer only from immutable input already supplied to the audit writer.

    This intentionally does not query a resource by id.  If the two snapshots
    disagree (for example, a membership moved branches), attribution is
    ambiguous and remains unresolved for organization-wide review.
    """
    return resolve_audit_scope(
        resource_type=resource_type,
        resource_id=resource_id,
        before=before,
        after=after,
    ).scope


def resolve_audit_scope(
    *,
    resource_type: str,
    resource_id: str | int = "",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditScopeResolution:
    """Return scope plus a reviewable reason for migration/report tooling."""
    if current_schema() == get_public_schema_name():
        return AuditScopeResolution(organization_audit_scope(), "public_schema")

    if _snapshots_cross_scope(resource_type, before, after):
        return AuditScopeResolution(unresolved_audit_scope(), "conflicting_snapshots")
    if any(_has_incomplete_scope_evidence(resource_type, snapshot) for snapshot in (before, after)):
        return AuditScopeResolution(unresolved_audit_scope(), "ambiguous_snapshot")

    candidates = {
        candidate
        for snapshot in (before, after)
        if (candidate := _scope_candidate(resource_type, resource_id, snapshot)) is not None
    }
    if len(candidates) == 1:
        branch_id, department_id = candidates.pop()
        return AuditScopeResolution(
            scoped_audit_scope(branch_id, department_id),
            "explicit_snapshot",
        )
    if candidates:
        return AuditScopeResolution(unresolved_audit_scope(), "conflicting_snapshots")
    if resource_type in _ORGANIZATION_RESOURCE_TYPES:
        return AuditScopeResolution(
            organization_audit_scope(),
            "organization_resource",
        )
    return AuditScopeResolution(unresolved_audit_scope(), "insufficient_evidence")


def audit_scope_for_instance(
    instance: Any,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditScopeSnapshot:
    """Resolve a receiver event from explicit relationships on the saved object."""
    resource_type = f"{instance._meta.app_label}.{instance.__class__.__name__}"
    resolution = resolve_audit_scope(
        resource_type=resource_type,
        resource_id=getattr(instance, "pk", "") or "",
        before=before,
        after=after,
    )
    if resolution.scope.status != UNRESOLVED:
        return resolution.scope
    # A relationship fallback is valid only when the snapshots simply lack a
    # direct scope field.  Never let a live relation overwrite an explicit
    # ambiguity/conflict: that would attribute a cross-scope move solely to the
    # resource's new owner and leak the old side of the mutation.
    if resolution.reason != "insufficient_evidence":
        return resolution.scope

    if resource_type == "academics.ExamResult":
        # ExamResult ownership is explicit through its immutable exam/cohort
        # relationship at the time of grading.  Freeze it now; never rejoin it
        # from the audit read path later.
        try:
            cohort = instance.exam.cohort
            return scoped_audit_scope(cohort.branch_id, cohort.department_id)
        except (AttributeError, TypeError, ValueError):
            return unresolved_audit_scope()

    if resource_type == "finance.Invoice":
        return _historical_instance_scope(
            instance,
            branch_attr="branch_at_issue_id",
            department_attr="department_at_issue_id",
        )
    if resource_type == "payments.Payment":
        return _historical_instance_scope(
            instance,
            branch_attr="branch_at_payment_id",
            department_attr="department_at_payment_id",
        )
    if resource_type == "teachers.PayoutPolicy":
        try:
            teacher = instance.teacher
            return scoped_audit_scope(teacher.branch_id, teacher.department_id)
        except (AttributeError, TypeError, ValueError):
            return unresolved_audit_scope()
    if resource_type in {"forms_app.Form", "meetings.StaffMeeting", "staff_tasks.Task"}:
        return _direct_workflow_scope(instance)
    if resource_type == "forms_app.FormField":
        return _related_branch_scope(instance, relation="form")
    if resource_type == "meetings.MeetingAttendee":
        return _related_branch_scope(instance, relation="meeting")
    return unresolved_audit_scope()


def _historical_instance_scope(
    instance: Any,
    *,
    branch_attr: str,
    department_attr: str,
) -> AuditScopeSnapshot:
    branch_id = getattr(instance, branch_attr, None)
    department_id = getattr(instance, department_attr, None)
    try:
        return scoped_audit_scope(cast(int, branch_id), department_id)
    except (TypeError, ValueError):
        return unresolved_audit_scope()


def _related_branch_scope(instance: Any, *, relation: str) -> AuditScopeSnapshot:
    """Freeze a direct parent workflow boundary while the row is being written.

    This lookup is intentionally confined to the write-side receiver.  Audit
    reads never rejoin the live parent, so moving or deleting a workflow later
    cannot move its history between branches.
    """
    try:
        parent = getattr(instance, relation)
        branch_id = parent.branch_id
        if branch_id is None:
            return organization_audit_scope()
        return scoped_audit_scope(branch_id)
    except (AttributeError, TypeError, ValueError):
        return unresolved_audit_scope()


def _direct_workflow_scope(instance: Any) -> AuditScopeSnapshot:
    """Resolve a tenant-wide or branch/department workflow at write time."""
    branch_id = getattr(instance, "branch_id", None)
    department_id = getattr(instance, "department_id", None)
    if branch_id is None:
        return organization_audit_scope() if department_id is None else unresolved_audit_scope()
    try:
        return scoped_audit_scope(branch_id, department_id)
    except (TypeError, ValueError):
        return unresolved_audit_scope()


def _scope_candidate(
    resource_type: str,
    resource_id: str | int,
    snapshot: dict[str, Any] | None,
) -> tuple[int, int | None] | None:
    if resource_type == "org.Branch":
        branch_id = _positive_int(resource_id)
        return (branch_id, None) if branch_id is not None else None
    if not isinstance(snapshot, dict):
        return None

    if resource_type == "org.Department":
        branch_id = _positive_int(snapshot.get("branch_id"))
        department_id = _positive_int(resource_id)
        return (branch_id, department_id) if branch_id is not None and department_id is not None else None
    if resource_type in {"org.Room", "org.BranchWorkingHours", "org.BranchHoliday"}:
        return _branch_department_candidate(snapshot, branch_key="branch_id")
    if resource_type in {"users.RoleMembership", "access.account_type_assignment"}:
        return _branch_department_candidate(
            snapshot,
            branch_key="branch_id",
            department_key="department_id",
        )
    if resource_type == "printing.PrintJob":
        return _branch_department_candidate(snapshot, branch_key="branch_id")
    if resource_type in {
        "approvals.ApprovalRequest",
        "approvals.LedgerEntry",
    }:
        return _branch_department_candidate(snapshot, branch_key="branch_id")
    if resource_type in {
        "forms_app.Form",
        "meetings.StaffMeeting",
    }:
        return _branch_department_candidate(snapshot, branch_key="branch_id")
    if resource_type == "staff_tasks.Task":
        return _branch_department_candidate(
            snapshot,
            branch_key="branch_id",
            department_key="department_id",
        )
    if resource_type == "teachers.TeacherProfile":
        return _branch_department_candidate(
            snapshot,
            branch_key="branch_id",
            department_key="department_id",
        )
    if resource_type == "finance.Invoice":
        return _branch_department_candidate(
            snapshot,
            branch_key="branch_at_issue_id",
            department_key="department_at_issue_id",
        )
    if resource_type == "payments.Payment":
        return _branch_department_candidate(
            snapshot,
            branch_key="branch_at_payment_id",
            department_key="department_at_payment_id",
        )
    return None


def _has_incomplete_scope_evidence(
    resource_type: str,
    snapshot: dict[str, Any] | None,
) -> bool:
    """Detect update diffs that mention a department but omit its branch.

    Older audit writers stored only changed ``after`` keys.  A department-only
    move therefore cannot safely reuse the full ``before`` branch/department
    candidate; doing so would silently attribute the change to the old scope.
    """
    if not isinstance(snapshot, dict):
        return False
    key_pairs = {
        "users.RoleMembership": ("branch_id", "department_id"),
        "access.account_type_assignment": ("branch_id", "department_id"),
        "finance.Invoice": ("branch_at_issue_id", "department_at_issue_id"),
        "payments.Payment": ("branch_at_payment_id", "department_at_payment_id"),
        "teachers.TeacherProfile": ("branch_id", "department_id"),
    }
    keys = key_pairs.get(resource_type)
    if keys is None:
        return False
    branch_key, department_key = keys
    return department_key in snapshot and branch_key not in snapshot


def _snapshots_cross_scope(
    resource_type: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> bool:
    """Treat moves, including tenant-wide-to-branch moves, as unresolved.

    A single update can contain information from both organizational scopes. It
    must remain visible only to organization-wide reviewers instead of being
    attributed to whichever non-null branch happened to survive candidate
    extraction.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    scope_keys = {
        "org.Department": ("branch_id",),
        "users.RoleMembership": ("branch_id", "department_id"),
        "access.account_type_assignment": ("branch_id", "department_id"),
        "forms_app.Form": ("branch_id",),
        "meetings.StaffMeeting": ("branch_id",),
        "staff_tasks.Task": ("branch_id", "department_id"),
        "teachers.TeacherProfile": ("branch_id", "department_id"),
        "finance.Invoice": ("branch_at_issue_id", "department_at_issue_id"),
        "payments.Payment": ("branch_at_payment_id", "department_at_payment_id"),
    }.get(resource_type)
    if scope_keys is None or not all(key in before and key in after for key in scope_keys):
        return False
    return any(before.get(key) != after.get(key) for key in scope_keys)


def _branch_department_candidate(
    snapshot: dict[str, Any],
    *,
    branch_key: str,
    department_key: str | None = None,
) -> tuple[int, int | None] | None:
    branch_id = _positive_int(snapshot.get(branch_key))
    if branch_id is None:
        return None
    department_id = _positive_int(snapshot.get(department_key)) if department_key else None
    return branch_id, department_id


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
