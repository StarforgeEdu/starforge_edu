"""Tasks + role hierarchy (F5-1/2/3).

`RoleGrade` is a per-center ranking of roles (higher level = more senior); it
drives hierarchy-gated assignment — you may task only equal/lower grades unless
you hold the bypass permission. `Task` is a unit of work assigned to a person or
a whole department, with a small status lifecycle.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class RoleGrade(models.Model):
    """One role's seniority level for this center. Roles without a grade are
    treated as level 0 (most junior) by the assignment gate."""

    role = models.CharField(max_length=32, unique=True)  # one of core.permissions.Role.ALL
    level = models.PositiveIntegerField()  # higher = more senior
    label = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-level", "role")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.role}={self.level}"


class Task(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        IN_PROGRESS = "in_progress", _("In progress")
        BLOCKED = "blocked", _("Blocked")
        DONE = "done", _("Done")
        CANCELLED = "cancelled", _("Cancelled")

    class Priority(models.TextChoices):
        LOW = "low", _("Low")
        NORMAL = "normal", _("Normal")
        HIGH = "high", _("High")
        URGENT = "urgent", _("Urgent")

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN, db_index=True)
    priority = models.CharField(max_length=8, choices=Priority.choices, default=Priority.NORMAL)
    # A task targets a person and/or a whole department (either may be null —
    # both null is an unassigned backlog item).
    assignee = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_tasks"
    )
    department = models.ForeignKey(
        "org.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks"
    )
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="tasks"
    )
    due_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("status", "due_at")),
            models.Index(fields=("assignee", "status")),
            models.Index(fields=("department", "status")),
            # A director's unscoped task board is whole-tenant, newest-first; the composites
            # all lead with status/assignee/department, so index the default created_at sort.
            models.Index(fields=("-created_at", "id"), name="task_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"task#{self.pk}:{self.title}:{self.status}"
