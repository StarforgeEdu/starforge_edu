"""Task-domain presenters — plain dict mappers (replace the DRF serializers)."""

from __future__ import annotations

from typing import Any

from apps.tasks.models import RoleGrade, Task


def task_to_dict(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "priority": t.priority,
        "assignee": t.assignee_id,
        "department": t.department_id,
        "branch": t.branch_id,
        "due_at": t.due_at.isoformat() if t.due_at else None,
        "created_by": t.created_by_id,
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
