"""Attendance response presenters (formerly the DRF serializers' output shape)."""

from __future__ import annotations

from apps.attendance.models import AttendanceRecord


def record_to_dict(record: AttendanceRecord) -> dict:
    """Flat record shape with denormalized `student_name`/`lesson_title`, resolved
    from the selector's select_related("student__user", "lesson") — no extra query
    per row."""
    lesson = record.lesson
    return {
        "id": record.id,
        "student": record.student_id,
        "student_name": record.student.get_full_name(),
        "lesson": record.lesson_id,
        "lesson_title": lesson.title,
        "lesson_starts_at": lesson.starts_at.isoformat(),
        # The group + teacher live on the lesson — surfaced here so an attendance row
        # answers "which group / which teacher" without a second call (owner ask).
        "cohort": lesson.cohort_id,
        "cohort_name": lesson.cohort.name,
        "teacher": lesson.teacher_id,
        "teacher_name": lesson.teacher.get_full_name(),
        "status": record.status,
        "arrived_at": record.arrived_at.isoformat() if record.arrived_at else None,
        "note": record.note,
        "marked_by": record.marked_by_id,
        "marked_at": record.marked_at.isoformat() if record.marked_at else None,
        "auto_marked": record.auto_marked,
        "created_at": record.created_at.isoformat(),
    }
