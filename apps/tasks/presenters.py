"""Task-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ObjectDoesNotExist

from apps.tasks.models import RoleGrade, Task


def _assignee_identity(task: Task) -> dict[str, Any] | None:
    if (
        task.assignee is None
        or task.assignee_attribution_status != "captured"
        or task.assignee_principal_kind not in {"staff", "teacher"}
        or task.assignee_principal_id is None
    ):
        return None
    relation = "staff_profile" if task.assignee_principal_kind == "staff" else "teacher_profile"
    try:
        profile = getattr(task.assignee, relation)
    except ObjectDoesNotExist:
        return None
    if profile.pk != task.assignee_principal_id:
        return None
    display_name = (
        " ".join(
            value.strip()
            for value in (profile.first_name, profile.middle_name, profile.last_name)
            if isinstance(value, str) and value.strip()
        )
        or profile.username
    )
    return {
        "kind": task.assignee_principal_kind,
        "id": task.assignee_principal_id,
        "display_name": display_name,
        "account_label": "Teacher" if task.assignee_principal_kind == "teacher" else "Staff",
    }


def _creator_identity(task: Task) -> dict[str, Any] | None:
    if (
        task.created_by_attribution_status
        not in {
            Task.CreatorAttributionStatus.CAPTURED,
            Task.CreatorAttributionStatus.RESOLVED,
        }
        or task.created_by_principal_kind not in {"staff", "teacher"}
        or task.created_by_principal_id is None
    ):
        return None
    display_name = None
    if task.created_by is not None:
        relation = "staff_profile" if task.created_by_principal_kind == "staff" else "teacher_profile"
        try:
            profile = getattr(task.created_by, relation)
        except ObjectDoesNotExist:
            profile = None
        if profile is not None and profile.pk == task.created_by_principal_id:
            display_name = (
                " ".join(
                    value.strip()
                    for value in (profile.first_name, profile.middle_name, profile.last_name)
                    if isinstance(value, str) and value.strip()
                )
                or profile.username
            )
    return {
        "kind": task.created_by_principal_kind,
        "id": task.created_by_principal_id,
        "display_name": display_name,
        "account_label": "Teacher" if task.created_by_principal_kind == "teacher" else "Staff",
    }


def task_to_dict(t: Task) -> dict[str, Any]:
    assignee_identity = _assignee_identity(t)
    creator_identity = _creator_identity(t)
    assignee_is_attributed = assignee_identity is not None
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "priority": t.priority,
        # An ambiguous legacy bridge id is quarantined, never exposed as if it
        # identified the actual account that owns the task.
        "assignee": t.assignee_id if assignee_is_attributed else None,
        "assignee_principal": assignee_identity,
        "assignee_name": assignee_identity["display_name"] if assignee_identity is not None else None,
        "assignee_attribution_status": t.assignee_attribution_status,
        "department": t.department_id,
        "department_name": t.department.name if t.department is not None else None,
        "branch": t.branch_id,
        "branch_name": t.branch.name if t.branch is not None else None,
        "due_at": t.due_at.isoformat() if t.due_at else None,
        "created_by": creator_identity,
        "created_by_name": (creator_identity["display_name"] if creator_identity is not None else None),
        "created_by_attribution_status": t.created_by_attribution_status,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "created_at": t.created_at.isoformat(),
    }


def role_grade_to_dict(g: RoleGrade) -> dict[str, Any]:
    return {
        "id": g.id,
        "role": g.role,
        "level": g.level,
        "label": g.label,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
    }
