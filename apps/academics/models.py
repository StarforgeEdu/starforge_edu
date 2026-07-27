"""Academics models (TASKS §11): subjects, exams + results, computed term
grades, and transcript jobs.

Exam results roll up into a per-(student, subject, term) `Grade` whose
`value_display` is rendered per the Center's grading scheme (TD-13). Transcripts
are generated off-request by a Celery task (TD-14, weasyprint → S3).
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class Subject(models.Model):
    name = models.CharField(max_length=200)
    code = models.SlugField(max_length=50, unique=True)
    department = models.ForeignKey(
        "org.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="subjects"
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.code}:{self.name}"


class ExamType(models.Model):
    """Dynamic, manager-created exam kind (per-Center configurable): "Midterm",
    "Final", "Quiz", "Speaking", "Mock IELTS", … — every education center names
    its assessments differently, so the kinds are data, not a hardcoded enum. The
    five defaults (midterm/final/quiz/project/oral) are seeded per tenant. Mirrors
    ``schedule.LessonType``."""

    name = models.CharField(max_length=64)
    slug = models.SlugField(max_length=64, unique=True)
    color = models.CharField(max_length=16, blank=True)  # optional UI hint, e.g. "#3b82f6"
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class Exam(models.Model):
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="exams")
    cohort = models.ForeignKey("cohorts.Cohort", on_delete=models.PROTECT, related_name="exams")
    term = models.ForeignKey("schedule.Term", on_delete=models.PROTECT, related_name="exams")
    # Per-Center configurable kind (SET_NULL preserves the exam if a center retires a
    # type, like schedule.Lesson.lesson_type).
    exam_type = models.ForeignKey(
        ExamType, on_delete=models.SET_NULL, null=True, blank=True, related_name="exams"
    )
    title = models.CharField(max_length=200)
    exam_date = models.DateField()
    max_score = models.DecimalField(max_digits=6, decimal_places=2, default=100)
    weight = models.DecimalField(max_digits=4, decimal_places=3, default=1)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-exam_date",)
        indexes = [
            models.Index(fields=("cohort", "term")),
            models.Index(fields=("subject", "term")),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(max_score__gt=0), name="exam_max_score_positive"),
            models.CheckConstraint(condition=Q(weight__gt=0), name="exam_weight_positive"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} ({self.subject_id})"


class ExamResult(models.Model):
    """A per-(exam, student) raw score.

    `graded_at` uses `auto_now`, so it tracks the LAST-MODIFIED time (it is reset
    on every overwrite), not the original first-graded timestamp. Audit/transcript
    consumers must treat it as "last edited", not "first entered" — the
    `grade_changed` signal (services.record_results) carries the change event when
    the original-entry moment matters.
    """

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name="results")
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.PROTECT, related_name="exam_results"
    )
    score = models.DecimalField(max_digits=6, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)
    graded_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    graded_at = models.DateTimeField(auto_now=True)  # last-modified (auto_now), not first-graded

    class Meta:
        ordering = ("-graded_at",)
        constraints = [
            models.UniqueConstraint(fields=("exam", "student"), name="examresult_unique_exam_student"),
            models.CheckConstraint(condition=Q(score__gte=0), name="examresult_score_nonneg"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.exam_id}:{self.student_id}={self.score}"


class Grade(models.Model):
    """A computed per-(student, subject, term) term grade. `value_raw` is the
    weighted 0-100 score; `value_display` is rendered per the active scheme."""

    student = models.ForeignKey("students.StudentProfile", on_delete=models.PROTECT, related_name="grades")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="grades")
    term = models.ForeignKey("schedule.Term", on_delete=models.PROTECT, related_name="grades")
    value_raw = models.DecimalField(max_digits=6, decimal_places=3)
    value_display = models.CharField(max_length=8)
    components = models.JSONField(default=list)  # [{exam, title, score, max_score, weight}]
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-computed_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("student", "subject", "term"), name="grade_unique_student_subject_term"
            ),
        ]
        indexes = [models.Index(fields=("student", "term"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}:{self.subject_id}:{self.term_id}={self.value_display}"


class Transcript(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        PROCESSING = "processing", _("Processing")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.PROTECT, related_name="transcripts"
    )
    term = models.ForeignKey(
        "schedule.Term", on_delete=models.PROTECT, null=True, blank=True, related_name="transcripts"
    )  # null = full academic history
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    pdf_key = models.CharField(max_length=512, blank=True)
    error = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    generated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"transcript#{self.pk}:{self.status}"
