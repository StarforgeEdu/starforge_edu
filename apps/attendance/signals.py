"""Attendance domain signals (emit-only today; D3-C consumes for guardian
SMS / in-app notify, D4-C `AttendanceConsumer` for live dashboards).

Flat primitive kwargs + ``schema_name`` for cross-context dispatch — receivers
re-enter the right tenant schema from the string.

`student_marked_absent` fires once per record that *becomes* absent, whether by
a manual teacher mark (`mark_attendance`) or the auto-absent sweep
(`mark_absent_after_lesson`). Signature:

    student_marked_absent.send(
        sender=AttendanceRecord,
        record_id=int,
        student_id=int,
        lesson_id=int,
        auto=bool,            # True for the beat-task sweep, False for a manual mark
        schema_name=str,
    )
"""

from __future__ import annotations

import django.dispatch

student_marked_absent = django.dispatch.Signal()
