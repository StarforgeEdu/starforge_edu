"""Cohort (class group) domain models (TASKS §8)."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class Cohort(models.Model):
    name = models.CharField(max_length=120)
    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="cohorts")
    department = models.ForeignKey(
        "org.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cohorts",
    )
    level = models.CharField(max_length=64, blank=True)
    start_date = models.DateField()
    end_date = models.DateField()
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)
    primary_teacher = models.ForeignKey(
        "teachers.TeacherProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_cohorts",
    )
    default_room = models.ForeignKey(
        "org.Room", on_delete=models.SET_NULL, null=True, blank=True, related_name="cohorts"
    )
    is_archived = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("branch", "name"),)
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class CohortMembership(models.Model):
    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, related_name="memberships")
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.CASCADE, related_name="cohort_memberships"
    )
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    moved_reason = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("-start_date",)
        constraints = [
            models.UniqueConstraint(
                fields=["cohort", "student"],
                condition=Q(end_date__isnull=True),
                name="one_active_membership_per_cohort_student",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.cohort_id}:{self.student_id}"


class CohortTeacher(models.Model):
    class TeachRole(models.TextChoices):
        CO_TEACHER = "co_teacher", _("Co-teacher")
        ASSISTANT = "assistant", _("Assistant")

    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, related_name="co_teachers")
    teacher = models.ForeignKey(
        "teachers.TeacherProfile", on_delete=models.CASCADE, related_name="co_teaching"
    )
    role = models.CharField(max_length=16, choices=TeachRole.choices, default=TeachRole.CO_TEACHER)

    class Meta:
        unique_together = (("cohort", "teacher"),)
        ordering = ("cohort", "teacher")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.cohort_id}:{self.teacher_id}"
