"""Lesson cover requests (F18-1).

A teacher who can't take a lesson raises a `CoverRequest`. A manager either ASSIGNS
a specific cover teacher or OPENS it to the branch's teacher pool, where any teacher
may CLAIM it. On approval the lesson's `teacher` is actually reassigned (the cover is
real — the new teacher takes attendance), unless that teacher is already busy then.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class CoverRequest(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")
        CANCELLED = "cancelled", _("Cancelled")

    lesson = models.ForeignKey("schedule.Lesson", on_delete=models.CASCADE, related_name="cover_requests")
    requester = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="cover_requests"
    )
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    # True once a manager opens it to the pool (any teacher may then claim it).
    pool = models.BooleanField(default=False)
    cover_teacher = models.ForeignKey(
        "teachers.TeacherProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="covers"
    )
    # Denormalized from the lesson's cohort at creation, for branch-scoped visibility.
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="cover_requests"
    )
    decided_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("status", "pool")),
            models.Index(fields=("branch", "status")),
        ]
        constraints = [
            # At most one PENDING (open) cover request per lesson. Once a cover is
            # approved the swap is recorded on the lesson itself, so the row is
            # historical and must not block a fresh request (e.g. the cover teacher
            # later also falls ill -> re-cover). Cancelled/rejected free it too.
            models.UniqueConstraint(
                fields=("lesson",),
                condition=models.Q(status="open"),
                name="one_live_cover_per_lesson",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"cover#{self.pk}:lesson#{self.lesson_id}:{self.status}"
