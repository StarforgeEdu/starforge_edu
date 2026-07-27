"""Attendance models (TASKS §10): one record per (student, lesson).

`AttendanceRecord` is keyed to `schedule.Lesson`; teachers mark it through the
`mark_attendance` service (late threshold + correction window from
`CenterSettings`), and the `mark_absent_after_lesson` beat task back-fills
`absent` rows for no-shows.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class AttendanceRecord(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", _("Present")
        ABSENT = "absent", _("Absent")
        LATE = "late", _("Late")
        EXCUSED = "excused", _("Excused")

    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.PROTECT, related_name="attendance_records"
    )
    lesson = models.ForeignKey("schedule.Lesson", on_delete=models.PROTECT, related_name="attendance_records")
    status = models.CharField(max_length=8, choices=Status.choices)
    arrived_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    # SET_NULL preserves the historical record if the marking user is deleted;
    # null also denotes the auto-absent sweep (no human marker).
    marked_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    marked_at = models.DateTimeField(auto_now=True)
    auto_marked = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("student", "lesson"), name="attendance_unique_student_lesson"),
        ]
        indexes = [
            models.Index(fields=("lesson",)),
            models.Index(fields=("student", "created_at")),
            models.Index(fields=("status",)),
            # The staff-wide records list is newest-first and often unfiltered; the
            # (student, created_at) composite can't serve a global created_at sort.
            # AttendanceRecord is the fastest-growing table (students x lessons) — index it.
            models.Index(fields=("-created_at", "id"), name="attnrec_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}@{self.lesson_id}:{self.status}"
