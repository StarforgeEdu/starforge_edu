"""Attendance read selectors: role-scoped record queries, term summary, and the
single-query cohort dashboard."""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet

from apps.attendance.models import AttendanceRecord
from core.permissions import Role

# Who sees every record in the tenant. TEACHER is deliberately NOT here: a
# teacher is scoped to the lessons they teach (D2-B-4 "teacher only their
# cohorts'"). REGISTRAR/IT have no `attendance:read` in the matrix, so they
# never reach these selectors.
STAFF_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT}


def _base() -> QuerySet[AttendanceRecord]:
    # lesson__cohort (the group) + lesson__teacher__user (the teacher) are surfaced in
    # the presenter, so join them here — no extra query per row.
    return AttendanceRecord.objects.select_related(
        "student__user", "lesson__cohort", "lesson__teacher__user"
    )


def scoped_records(*, user, roles: set[str] | None = None) -> QuerySet[AttendanceRecord]:
    """Records visible to `user`: staff → all; teacher → records on lessons they
    teach; parent → guardian-linked children's; student → own."""
    qs = _base()
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.TEACHER in roles:
        return qs.filter(lesson__teacher__user=user)
    if Role.PARENT in roles:
        return qs.filter(student__guardians__parent__user=user).distinct()
    if Role.STUDENT in roles:
        return qs.filter(student__user=user)
    return qs.none()


def term_summary(*, base_qs: QuerySet[AttendanceRecord], student_id: int, term_id: int) -> dict:
    """Per-student per-term counts + `percent_present` (present / total * 100).

    `base_qs` is a scoped queryset, so a student/parent asking for someone else's
    summary gets all-zeros rather than a leak. Single aggregate query."""
    counts = base_qs.filter(student_id=student_id, lesson__term_id=term_id).aggregate(
        present=Count("id", filter=Q(status=AttendanceRecord.Status.PRESENT)),
        absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
        late=Count("id", filter=Q(status=AttendanceRecord.Status.LATE)),
        excused=Count("id", filter=Q(status=AttendanceRecord.Status.EXCUSED)),
        total=Count("id"),
    )
    total = counts.pop("total")
    counts["percent_present"] = round(100 * counts["present"] / total, 1) if total else 0.0
    return counts


def cohort_dashboard(*, cohort_id: int, date_from=None, date_to=None) -> dict:
    """Per-student present/absent/late/excused counts + rate for one cohort, plus
    a cohort-wide rate. ONE aggregate query (≤5 for the whole request, DoD)."""
    qs = AttendanceRecord.objects.filter(lesson__cohort_id=cohort_id)
    if date_from is not None:
        qs = qs.filter(lesson__starts_at__gte=date_from)
    if date_to is not None:
        qs = qs.filter(lesson__starts_at__lte=date_to)

    rows = list(
        qs.values(
            "student_id",
            "student__student_id",
            "student__user__first_name",
            "student__user__last_name",
        )
        .annotate(
            present=Count("id", filter=Q(status=AttendanceRecord.Status.PRESENT)),
            absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
            late=Count("id", filter=Q(status=AttendanceRecord.Status.LATE)),
            excused=Count("id", filter=Q(status=AttendanceRecord.Status.EXCUSED)),
            total=Count("id"),
        )
        .order_by("student__user__last_name", "student__user__first_name")
    )

    students = []
    cohort_present = cohort_total = 0
    for row in rows:
        total = row["total"]
        cohort_present += row["present"]
        cohort_total += total
        name = f"{row['student__user__first_name']} {row['student__user__last_name']}".strip()
        students.append(
            {
                "student": row["student_id"],
                "student_code": row["student__student_id"],
                "name": name,
                "present": row["present"],
                "absent": row["absent"],
                "late": row["late"],
                "excused": row["excused"],
                "total": total,
                "percent_present": round(100 * row["present"] / total, 1) if total else 0.0,
            }
        )
    cohort_rate = round(100 * cohort_present / cohort_total, 1) if cohort_total else 0.0
    return {"cohort": cohort_id, "rate": cohort_rate, "students": students}
