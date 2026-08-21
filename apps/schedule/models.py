"""Schedule models (TASKS section 9, TD-12): materialized-occurrence scheduling.

Recurrence rules expand to concrete `Lesson` rows; conflicts are caught both by
the `check_conflicts` selector (friendly 409) and by Postgres GiST exclusion
constraints (the last line of defence, even against raw ORM writes).
"""

from __future__ import annotations

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import Func, Q
from django.utils.translation import gettext_lazy as _


class TsTzRange(Func):
    """`tstzrange(starts_at, ends_at, '[)')` for range-overlap exclusion."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Term(models.Model):
    name = models.CharField(max_length=100)
    academic_year = models.CharField(max_length=9)  # e.g. "2026-2027"
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-start_date",)
        constraints = [
            models.UniqueConstraint(fields=("academic_year", "name"), name="term_unique_year_name"),
            models.UniqueConstraint(
                fields=("is_current",),
                condition=Q(is_current=True),
                name="term_one_current",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=models.F("start_date")), name="term_dates_ordered"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.academic_year} {self.name}"


class TimeSlot(models.Model):
    branch = models.ForeignKey("org.Branch", on_delete=models.CASCADE, related_name="time_slots")
    name = models.CharField(max_length=50)
    start_time = models.TimeField()
    end_time = models.TimeField()
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("branch", "order", "start_time")
        constraints = [
            models.UniqueConstraint(fields=("branch", "name"), name="timeslot_unique_branch_name"),
            models.CheckConstraint(
                condition=Q(end_time__gt=models.F("start_time")), name="timeslot_times_ordered"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch_id}:{self.name}"


class RecurrenceRule(models.Model):
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="rules")
    cohort = models.ForeignKey("cohorts.Cohort", on_delete=models.PROTECT, related_name="rules")
    teacher = models.ForeignKey("teachers.TeacherProfile", on_delete=models.PROTECT, related_name="rules")
    room = models.ForeignKey(
        "org.Room", on_delete=models.PROTECT, null=True, blank=True, related_name="rules"
    )
    lesson_type = models.ForeignKey(
        "schedule.LessonType", on_delete=models.SET_NULL, null=True, blank=True, related_name="rules"
    )
    title = models.CharField(max_length=200)
    rrule = models.TextField()  # RFC 5545 RRULE, validated by parsing
    start_date = models.DateField()
    end_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=models.F("start_date")), name="rule_dates_ordered"
            ),
            models.CheckConstraint(
                condition=Q(end_time__gt=models.F("start_time")), name="rule_times_ordered"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.title


class LessonType(models.Model):
    """Dynamic, manager-created lesson kind (F3-1): "Video Lesson", "Speaking",
    "Main", "Hangout", … — defined per Center so it fits any curriculum."""

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


class Lesson(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        CANCELLED = "cancelled", _("Cancelled")
        COMPLETED = "completed", _("Completed")
        ARCHIVED = "archived", _("Archived")

    rule = models.ForeignKey(
        RecurrenceRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="lessons"
    )
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="lessons")
    cohort = models.ForeignKey("cohorts.Cohort", on_delete=models.PROTECT, related_name="lessons")
    teacher = models.ForeignKey("teachers.TeacherProfile", on_delete=models.PROTECT, related_name="lessons")
    room = models.ForeignKey(
        "org.Room", on_delete=models.PROTECT, null=True, blank=True, related_name="lessons"
    )
    lesson_type = models.ForeignKey(
        LessonType, on_delete=models.SET_NULL, null=True, blank=True, related_name="lessons"
    )
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    detached_from_rule = models.BooleanField(default=False)
    cancel_reason = models.CharField(max_length=255, blank=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # Set once the post-start absence sweep has reconciled this lesson's historical
    # roster.  Without this marker every 15-minute beat re-read every past lesson for
    # the whole term, producing an ever-growing 2N+1 no-op query load.
    auto_absence_processed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("starts_at",)
        indexes = [
            models.Index(fields=("cohort", "starts_at")),
            models.Index(fields=("teacher", "starts_at")),
            models.Index(fields=("room", "starts_at")),
            models.Index(
                fields=("status", "auto_absence_processed_at", "starts_at"),
                name="lesson_auto_absence_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="lesson_times_ordered"
            ),
            # Last line of defence: no two SCHEDULED lessons may overlap in time
            # for the same room / teacher / cohort — enforced even against raw
            # ORM writes (TD-12). Needs btree_gist (added in the migration).
            ExclusionConstraint(
                name="lesson_no_room_overlap",
                expressions=[
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                    ("room", RangeOperators.EQUAL),
                ],
                condition=Q(status="scheduled", room__isnull=False),
            ),
            ExclusionConstraint(
                name="lesson_no_teacher_overlap",
                expressions=[
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                    ("teacher", RangeOperators.EQUAL),
                ],
                condition=Q(status="scheduled"),
            ),
            ExclusionConstraint(
                name="lesson_no_cohort_overlap",
                expressions=[
                    (TsTzRange("starts_at", "ends_at", RangeBoundary()), RangeOperators.OVERLAPS),
                    ("cohort", RangeOperators.EQUAL),
                ],
                condition=Q(status="scheduled"),
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} @ {self.starts_at:%Y-%m-%d %H:%M}"
