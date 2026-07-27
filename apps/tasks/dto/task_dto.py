"""Task-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CreateTaskDTO:
    title: str
    description: str = ""
    priority: str = "normal"
    assignee_id: int | None = None
    department_id: int | None = None
    branch_id: int | None = None
    due_at: datetime | None = None


@dataclass(frozen=True)
class AssignTaskDTO:
    # `*_provided` distinguishes "key absent" from an explicit null (clear the field) —
    # mirrors the old TaskAssignSerializer (either/both may be given; at least one).
    assignee_provided: bool = False
    assignee_id: int | None = None
    department_provided: bool = False
    department_id: int | None = None


@dataclass(frozen=True)
class RoleGradeDTO:
    role: str
    level: int
    label: str = ""
