"""Custom achievements (F15-2) — positive recognition for students (dignity DNA).

A manager defines GLOBAL (center-wide) achievements; a teacher defines GROUP
(cohort-scoped) ones for their own class. A teacher may also REQUEST a global
achievement, which a manager approves before it goes live. Active achievements are
GRANTED to students (an `AchievementGrant`) — the student's wall of recognition.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Achievement(models.Model):
    class Scope(models.TextChoices):
        GROUP = "group", _("Group")
        GLOBAL = "global", _("Global")

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PENDING = "pending", _("Pending approval")
        REJECTED = "rejected", _("Rejected")

    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    emoji = models.CharField(max_length=32, blank=True)  # fits multi-codepoint ZWJ emoji
    scope = models.CharField(max_length=8, choices=Scope.choices)
    # Required for GROUP scope (which class it belongs to); null for GLOBAL.
    cohort = models.ForeignKey(
        "cohorts.Cohort", on_delete=models.CASCADE, null=True, blank=True, related_name="achievements"
    )
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="achievements"
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="created_achievements"
    )
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("scope", "status"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.scope}/{self.status})"


class AchievementGrant(models.Model):
    achievement = models.ForeignKey(Achievement, on_delete=models.CASCADE, related_name="grants")
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.CASCADE, related_name="achievement_grants"
    )
    granted_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    note = models.CharField(max_length=255, blank=True)
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-granted_at",)
        indexes = [models.Index(fields=("student", "granted_at"))]
        constraints = [
            models.UniqueConstraint(
                fields=("achievement", "student"), name="one_grant_per_achievement_per_student"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"grant:{self.achievement_id}->student#{self.student_id}"
