"""Persistent, tenant-scoped review state for student and teacher imports.

The uploaded source file is deliberately not retained. Only the normalized rows
needed for review are stored, inside the tenant schema, and every draft is bound
to the exact role principal that uploaded it.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class PeopleImportDraft(models.Model):
    class Kind(models.TextChoices):
        STUDENT = "student", "Students"
        TEACHER = "teacher", "Teachers"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        NEEDS_ATTENTION = "needs_attention", "Needs attention"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    source_file_name = models.CharField(max_length=255)
    source_sheet = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="people_import_drafts",
    )
    principal_kind = models.CharField(max_length=16)
    principal_id = models.PositiveBigIntegerField()
    default_branch_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    row_count = models.PositiveIntegerField(default=0)
    ready_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    excluded_count = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    error_message = models.CharField(max_length=500, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes = [
            models.Index(fields=("kind", "status", "-updated_at"), name="people_import_kind_state_idx"),
            models.Index(
                fields=("created_by", "principal_kind", "principal_id", "-updated_at"),
                name="people_import_owner_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"{self.get_kind_display()}: {self.source_file_name}"


class PeopleImportRow(models.Model):
    class State(models.TextChoices):
        READY = "ready", "Ready"
        INVALID = "invalid", "Needs attention"
        IMPORTED = "imported", "Imported"

    draft = models.ForeignKey(PeopleImportDraft, on_delete=models.CASCADE, related_name="rows")
    position = models.PositiveIntegerField()
    source_data = models.JSONField(default=dict)
    data = models.JSONField(default=dict)
    errors = models.JSONField(default=dict)
    state = models.CharField(
        max_length=16,
        choices=State.choices,
        default=State.READY,
        db_index=True,
    )
    is_included = models.BooleanField(default=True, db_index=True)
    created_object_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(fields=("draft", "position"), name="people_import_unique_row"),
        ]
        indexes = [
            models.Index(
                fields=("draft", "state", "is_included", "position"),
                name="people_import_row_state_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"{self.draft_id}:{self.position}"
