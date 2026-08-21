"""Assignments / homework models (TASKS §12).

An `Assignment` belongs to a cohort; students post `Submission`s (S3 attachment
keys + text), each graded once via a one-to-one `SubmissionGrade`. Late flag and
resubmit limits come from `CenterSettings` (TD-13).
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class AssignmentUploadGrant(models.Model):
    """Single-use, owner-bound authorization for an assignment S3 object."""

    key = models.CharField(max_length=512, unique=True)
    requested_by = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    content_type = models.CharField(max_length=127)
    expected_size_bytes = models.PositiveBigIntegerField()
    actual_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    source_deleted_at = models.DateTimeField(null=True, blank=True)
    durable_key = models.CharField(max_length=512, null=True, blank=True, unique=True)
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    durable_deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("requested_by", "consumed_at", "expires_at")),
            models.Index(
                fields=("source_deleted_at", "expires_at"),
                name="assign_upload_source_exp_idx",
            ),
            models.Index(
                fields=("durable_deleted_at", "deletion_requested_at"),
                name="assign_upload_delete_idx",
            ),
        ]


class Assignment(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")
        CLOSED = "closed", _("Closed")

    cohort = models.ForeignKey("cohorts.Cohort", on_delete=models.PROTECT, related_name="assignments")
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    due_at = models.DateTimeField()
    attachments = models.JSONField(default=list)  # S3 keys
    rubric = models.JSONField(default=list)  # [{criterion: str, max_points: int}]
    max_score = models.DecimalField(max_digits=6, decimal_places=2, default=100)
    max_resubmits = models.PositiveSmallIntegerField(null=True, blank=True)  # null = knob value
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True)
    due_soon_sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-due_at",)
        indexes = [models.Index(fields=("cohort", "due_at"))]

    def __str__(self) -> str:  # pragma: no cover
        return self.title


class Submission(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        GRADED = "graded", _("Graded")
        RETURNED = "returned", _("Returned")

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="submissions")
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.PROTECT, related_name="submissions"
    )
    text = models.TextField(blank=True)
    attachments = models.JSONField(default=list)
    submitted_at = models.DateTimeField(auto_now_add=True)
    is_late = models.BooleanField(default=False)
    attempt_number = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED)

    class Meta:
        ordering = ("-submitted_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("assignment", "student", "attempt_number"),
                name="submission_unique_assignment_student_attempt",
            ),
            models.CheckConstraint(condition=Q(attempt_number__gte=1), name="submission_attempt_positive"),
        ]
        indexes = [models.Index(fields=("assignment", "student"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.assignment_id}:{self.student_id}#{self.attempt_number}"


class SubmissionGrade(models.Model):
    submission = models.OneToOneField(Submission, on_delete=models.CASCADE, related_name="grade")
    score = models.DecimalField(max_digits=6, decimal_places=2)
    rubric_scores = models.JSONField(default=list)  # [{criterion: str, points: number}]
    feedback = models.TextField(blank=True)
    ai_feedback = models.TextField(blank=True)  # written by D4-A
    graded_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    graded_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(score__gte=0), name="submissiongrade_score_nonneg"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"grade#{self.submission_id}={self.score}"
