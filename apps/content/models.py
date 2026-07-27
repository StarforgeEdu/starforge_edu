"""Content library models (TASKS §13, §23).

A `ContentLibrary` (visibility-scoped) holds a Course → Module → ContentLesson
hierarchy and/or `Folder`s; both anchor `LessonFile`s. Files move through the
signed-URL upload state machine (pending → clean / rejected) — see
`apps/content/services.py`.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class ContentLibrary(models.Model):
    class Visibility(models.TextChoices):
        TENANT = "tenant", _("Everyone in the center")
        DEPARTMENT = "department", _("A department")
        COHORT = "cohort", _("A cohort")
        ROLE = "role", _("Specific roles")

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.TENANT)
    department = models.ForeignKey(
        "org.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="libraries"
    )
    cohort = models.ForeignKey(
        "cohorts.Cohort", on_delete=models.SET_NULL, null=True, blank=True, related_name="libraries"
    )
    allowed_roles = models.JSONField(default=list)  # role codes for visibility=role
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class LibraryMaterial(models.Model):
    """A text teaching material in a library (F9-1). A manager creates a DRAFT (title +
    topic), optionally has the AI draft its `body` (reusing the apps.ai pipeline), reviews
    / edits it, then PUBLISHES it so learners with access to the library can read it. The
    draft → published gate (paper-elimination DNA: AI drafts, a human still signs off)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        PUBLISHED = "published", _("Published")

    library = models.ForeignKey(ContentLibrary, on_delete=models.CASCADE, related_name="materials")
    title = models.CharField(max_length=200)
    # The author's brief for AI generation (what the material should cover).
    topic = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)  # the material text (AI-drafted and/or hand-edited)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("library", "status"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} ({self.status})"


class Course(models.Model):
    library = models.ForeignKey(ContentLibrary, on_delete=models.CASCADE, related_name="courses")
    subject = models.ForeignKey("academics.Subject", on_delete=models.PROTECT, related_name="courses")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("library", "order")

    def __str__(self) -> str:  # pragma: no cover
        return self.title


class Module(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="modules")
    title = models.CharField(max_length=200)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("course", "order")
        constraints = [
            models.UniqueConstraint(fields=("course", "order"), name="module_unique_course_order"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.title


class ContentLesson(models.Model):
    """Named to avoid clashing with `schedule.Lesson`."""

    module = models.ForeignKey(Module, on_delete=models.CASCADE, related_name="lessons")
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("module", "order")

    def __str__(self) -> str:  # pragma: no cover
        return self.title


class Folder(models.Model):
    library = models.ForeignKey(ContentLibrary, on_delete=models.CASCADE, related_name="folders")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="children"
    )
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ("library", "name")
        constraints = [
            models.UniqueConstraint(fields=("library", "parent", "name"), name="folder_unique_path"),
            models.UniqueConstraint(
                fields=("library", "name"),
                condition=Q(parent__isnull=True),
                name="folder_unique_root_name",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class LessonFile(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        CLEAN = "clean", _("Clean")
        REJECTED = "rejected", _("Rejected")

    lesson = models.ForeignKey(
        ContentLesson, on_delete=models.CASCADE, null=True, blank=True, related_name="files"
    )
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, null=True, blank=True, related_name="files")
    title = models.CharField(max_length=255)
    s3_key = models.CharField(max_length=512, unique=True)
    content_type = models.CharField(max_length=127)
    size_bytes = models.BigIntegerField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    reject_reason = models.CharField(max_length=255, blank=True)
    version = models.PositiveIntegerField(default=1)
    previous_version = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="next_versions"
    )
    thumbnail_key = models.CharField(max_length=512, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    download_count = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # F4-5 dual publication approval. A CLEAN file is published to learners only
    # after BOTH a teacher and a manager sign off (maker-checker: the two legs
    # must be different people; the manager leg requires a manager role + the
    # teacher leg already done). A new version is a fresh row, so it resets to
    # unapproved and must be re-signed before it reaches learners again.
    is_approved_teacher = models.BooleanField(default=False)
    approved_teacher_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_teacher_at = models.DateTimeField(null=True, blank=True)
    is_approved_manager = models.BooleanField(default=False)
    approved_manager_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    approved_manager_at = models.DateTimeField(null=True, blank=True)
    # F4-5 view-only toggle: when False the file streams in-app (track-view) but
    # no download URL is issued to learners (copy-control for exam/licensed work).
    is_downloadable = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(lesson__isnull=False) | Q(folder__isnull=False),
                name="lessonfile_lesson_or_folder",
            ),
        ]
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("folder",)),
            models.Index(fields=("lesson",)),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} ({self.status})"


class FileView(models.Model):
    class Action(models.TextChoices):
        VIEW = "view", _("View")
        DOWNLOAD = "download", _("Download")

    file = models.ForeignKey(LessonFile, on_delete=models.CASCADE, related_name="views")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="+")
    action = models.CharField(max_length=8, choices=Action.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("file", "created_at"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.file_id}:{self.action}"
