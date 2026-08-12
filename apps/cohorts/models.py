"""Cohort (class group) domain models (TASKS §8)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class Cohort(models.Model):
    class LessonCycleLength(models.IntegerChoices):
        EIGHT = 8, _("8 lessons")
        TWELVE = 12, _("12 lessons")

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
    # Human-readable position in the centre's curriculum. This is deliberately
    # explicit rather than guessed from dates: imported cohorts and holiday gaps
    # make calendar-month arithmetic untrustworthy.
    study_month = models.PositiveSmallIntegerField(default=1)
    # A cycle is an operational teaching cadence, not a level catalogue.  The
    # final slot is exposed as an exam-day signal; changing this value never
    # mutates the free-text ``level`` field.
    lesson_cycle_length = models.PositiveSmallIntegerField(
        choices=LessonCycleLength.choices,
        default=LessonCycleLength.TWELVE,
    )
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
        constraints = [
            models.CheckConstraint(
                condition=Q(study_month__gte=1, study_month__lte=600),
                name="cohort_study_month_supported",
            ),
            models.CheckConstraint(
                condition=Q(lesson_cycle_length__in=(8, 12)),
                name="cohort_lesson_cycle_length_supported",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        for field_name in ("department", "primary_teacher", "default_room"):
            related = getattr(self, field_name, None)
            if related is not None and self.branch_id and related.branch_id != self.branch_id:
                errors[field_name] = [str(_("Must belong to the cohort's branch."))]
        primary_teacher = self.primary_teacher
        if self.department_id and primary_teacher and primary_teacher.department_id != self.department_id:
            errors["primary_teacher"] = [str(_("Must belong to the cohort's department."))]
        if errors:
            raise ValidationError(errors)


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
    class LegacyRole(models.TextChoices):
        CO_TEACHER = "co_teacher", _("Co-teacher")
        ASSISTANT = "assistant", _("Assistant")

    cohort = models.ForeignKey(Cohort, on_delete=models.CASCADE, related_name="co_teachers")
    teacher = models.ForeignKey(
        "teachers.TeacherProfile", on_delete=models.CASCADE, related_name="co_teaching"
    )
    teacher_type = models.ForeignKey(
        "teachers.TeacherType",
        on_delete=models.PROTECT,
        related_name="cohort_assignments",
        # Expand/contract compatibility: old application nodes do not send this
        # column. The database trigger fills it from ``role``; new forms/services
        # still require it because blank remains False.
        null=True,
    )
    # Retained for one rolling-deploy compatibility window. New code treats
    # ``teacher_type`` as canonical and dual-writes this old two-value projection.
    role = models.CharField(
        max_length=16,
        choices=LegacyRole.choices,
        default=LegacyRole.CO_TEACHER,
    )

    class Meta:
        ordering = ("cohort", "teacher_type", "teacher")
        constraints = [
            models.UniqueConstraint(
                fields=("cohort", "teacher", "teacher_type"),
                name="unique_cohort_teacher_type",
            ),
            models.UniqueConstraint(
                fields=("cohort", "teacher"),
                condition=Q(teacher_type__isnull=True),
                name="unique_legacy_untyped_cohort_teacher",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.cohort_id}:{self.teacher_id}:{self.teacher_type_id}"

    def save(self, *args, **kwargs) -> None:
        """Dual-write the expand/contract compatibility columns in new nodes.

        A database trigger performs the same translation for old nodes, whose
        historical model is unaware of ``teacher_type``.
        """
        previous_type_id = self.teacher_type_id
        previous_role = self.role
        teacher_type = self.teacher_type if self.teacher_type_id else None
        if teacher_type is not None:
            self.role = (
                self.LegacyRole.ASSISTANT if teacher_type.slug == "assistant" else self.LegacyRole.CO_TEACHER
            )
        elif self.role:
            from apps.teachers.models import TeacherType

            type_slug = "assistant" if self.role == self.LegacyRole.ASSISTANT else "co-teacher"
            self.teacher_type_id = (
                TeacherType.objects.filter(slug=type_slug).values_list("pk", flat=True).first()
            )
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if self.teacher_type_id != previous_type_id:
                fields.add("teacher_type")
            if self.role != previous_role:
                fields.add("role")
            kwargs["update_fields"] = fields
        super().save(*args, **kwargs)

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        teacher_type = self.teacher_type if self.teacher_type_id else None
        if teacher_type is not None and not teacher_type.is_active:
            errors["teacher_type"] = [str(_("Choose an active teacher type."))]
        if self.cohort_id and self.teacher_id:
            if self.teacher.branch_id != self.cohort.branch_id:
                errors["teacher"] = [str(_("Must belong to the cohort's branch."))]
            elif self.cohort.department_id and self.teacher.department_id != self.cohort.department_id:
                errors["teacher"] = [str(_("Must belong to the cohort's department."))]
        if errors:
            raise ValidationError(errors)
