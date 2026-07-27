"""Student read selectors with role-based scoping (TD-5)."""

from __future__ import annotations

from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.students.models import EnrollmentEvent, StudentProfile
from core.permissions import Role

# What counts as "leaving" the center (for joined/left analytics).
_LEFT_STATUSES = (StudentProfile.Status.WITHDRAWN, StudentProfile.Status.GRADUATED)
_COMPARISON_UNITS = ("hour", "day", "week", "month", "year")

# Roles that see every student in the tenant.
STAFF_ROLES = {Role.DIRECTOR, Role.HEAD_OF_DEPT, Role.TEACHER, Role.REGISTRAR, Role.IT}


def _base_qs() -> QuerySet[StudentProfile]:
    return StudentProfile.objects.select_related("user", "branch", "current_cohort")


def scoped_students(*, user, roles: set[str] | None = None) -> QuerySet[StudentProfile]:
    qs = _base_qs()
    if user.is_superuser:
        return qs
    if roles is None:
        roles = {m.role for m in user.role_memberships.filter(revoked_at__isnull=True)}
    if roles & STAFF_ROLES:
        return qs
    if Role.PARENT in roles:  # read_own_children
        return qs.filter(guardians__parent__user=user).distinct()
    if Role.STUDENT in roles:  # read_self
        return qs.filter(user=user)
    return qs.none()  # fail closed


def students_with_upcoming_birthdays(
    *, base: QuerySet[StudentProfile] | None = None, days: int = 7, branch=None, cohort=None
) -> QuerySet[StudentProfile]:
    today = timezone.localdate()
    # Clamp defensively: the (month, day) set is exhaustive at 366 days anyway,
    # so capping is semantically lossless and protects future callers.
    month_days = {
        (today + timedelta(days=offset)).timetuple()[1:3] for offset in range(min(max(days, 0), 366) + 1)
    }
    window = Q()
    for month, day in month_days:
        window |= Q(birthdate__month=month, birthdate__day=day)
    qs = (base if base is not None else _base_qs()).filter(birthdate__isnull=False).filter(window)
    if branch:
        qs = qs.filter(branch_id=branch)
    if cohort:
        qs = qs.filter(current_cohort_id=cohort)
    return qs


def student_profile_for(user) -> StudentProfile | None:
    return StudentProfile.objects.select_related("current_cohort").filter(user=user).first()


def _classroom_rank(student: StudentProfile) -> dict | None:
    """The student's OWN standing in their cohort by average published-exam score —
    just their position + the cohort size, never classmates' names or scores (dignity
    DNA: a private report card, not a public leaderboard). None if they have no grades
    or no cohort to rank within."""
    from django.db.models import Avg, ExpressionWrapper, F, FloatField

    from apps.academics.models import ExamResult

    cohort = student.current_cohort
    if cohort is None:
        return None
    averages = {
        row["student_id"]: row["avg_pct"]
        for row in ExamResult.objects.filter(
            student__current_cohort=cohort,
            # Only currently-enrolled peers count — a withdrawn/graduated classmate
            # must not inflate the denominator or push a peer's rank down.
            student__status__in=(StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE),
            exam__is_published=True,
        )
        .values("student_id")
        .annotate(
            avg_pct=Avg(
                ExpressionWrapper(F("score") * 100.0 / F("exam__max_score"), output_field=FloatField())
            )
        )
    }
    mine = averages.get(student.id)
    if mine is None:
        return None  # ungraded students aren't ranked
    rank = 1 + sum(1 for other in averages.values() if other > mine)
    return {"rank": rank, "of": len(averages), "average_pct": round(mine, 1)}


def student_report(*, student: StudentProfile) -> dict:
    """The student-app report (F15-1): a per-lesson attendance sheet, the paid-status of
    their bills, and their own classroom rank — the three things a student checks daily,
    student-scoped, in one read."""
    from apps.attendance.models import AttendanceRecord
    from apps.finance.models import Invoice
    from apps.finance.selectors import outstanding_balance

    now = timezone.now()
    window = now - timedelta(days=90)  # a term-ish window
    St = AttendanceRecord.Status
    window_qs = AttendanceRecord.objects.filter(student=student, lesson__starts_at__gte=window)
    # The rate is over the WHOLE window (uncapped aggregate); only the per-lesson sheet
    # is capped, so a busy student's rate isn't silently computed over the last 100 rows.
    agg = window_qs.aggregate(
        counted=Count("id", filter=~Q(status=St.EXCUSED)),
        attended=Count("id", filter=Q(status__in=(St.PRESENT, St.LATE))),
    )
    counted, attended = agg["counted"], agg["attended"]
    attendance = {
        "rate": round(attended / counted, 3) if counted else None,
        "present": attended,
        "of": counted,
        "sheet": [
            {"date": r.lesson.starts_at, "lesson": r.lesson.title, "status": r.status}
            for r in window_qs.select_related("lesson").order_by("-lesson__starts_at")[:100]
        ],
    }

    latest = Invoice.objects.filter(student=student).order_by("-created_at").first()
    payment = {
        "outstanding_uzs": str(outstanding_balance(student.pk)),
        "has_overdue": Invoice.objects.filter(student=student, status=Invoice.Status.OVERDUE).exists(),
        "latest_invoice": (
            {
                "number": latest.number,
                "amount_uzs": str(latest.total_uzs),
                "status": latest.status,
                "due_date": latest.due_date,
            }
            if latest
            else None
        ),
    }

    # F15-1: a center can switch ranking off entirely (dignity) — then no rank is
    # computed or returned, on the student's own report and the parent view alike.
    from apps.org.selectors import get_center_settings

    rank = _classroom_rank(student) if get_center_settings().show_classroom_rank else None
    return {"attendance": attendance, "payment": payment, "rank": rank}


def student_dashboard(*, student: StudentProfile, user, roles) -> dict:
    """The signed-in student's cockpit (F4-1): their group, next lessons, open
    homework, recent published grades, outstanding balance, and outstanding rule
    acknowledgments — one read across the apps that already hold the data."""
    from apps.academics.models import ExamResult
    from apps.assignments.models import Assignment, Submission
    from apps.compliance import selectors as compliance_selectors
    from apps.finance.selectors import outstanding_balance
    from apps.schedule.models import Lesson

    now = timezone.now()
    cohort = student.current_cohort
    cohort_id = cohort.id if cohort else None

    next_lessons: list[dict] = []
    open_homework: list[dict] = []
    if cohort_id:
        next_lessons = [
            {
                "id": lesson.id,
                "title": lesson.title,
                "starts_at": lesson.starts_at,
                "lesson_type": lesson.lesson_type.name if lesson.lesson_type else None,
            }
            for lesson in Lesson.objects.filter(
                cohort_id=cohort_id, starts_at__gte=now, status=Lesson.Status.SCHEDULED
            )
            .select_related("lesson_type")
            .order_by("starts_at")[:5]
        ]
        submitted = set(Submission.objects.filter(student=student).values_list("assignment_id", flat=True))
        open_homework = [
            {"id": a.id, "title": a.title, "due_at": a.due_at}
            for a in Assignment.objects.filter(
                cohort_id=cohort_id, status=Assignment.Status.PUBLISHED, due_at__gte=now
            )
            .exclude(id__in=submitted)
            .order_by("due_at")[:10]
        ]

    recent_grades = [
        {
            "exam": result.exam.title,
            "score": str(result.score),
            "max_score": str(result.exam.max_score),
            "exam_date": result.exam.exam_date,
        }
        for result in ExamResult.objects.filter(student=student, exam__is_published=True)
        .select_related("exam")
        .order_by("-exam__exam_date")[:5]
    ]

    return {
        "group": cohort.name if cohort else None,
        "level": cohort.level if cohort else None,
        "next_lessons": next_lessons,
        "open_homework": open_homework,
        "open_homework_count": len(open_homework),
        "recent_grades": recent_grades,
        "outstanding_uzs": str(outstanding_balance(student.pk)),
        "pending_rule_acknowledgments": len(compliance_selectors.pending_rules(user, roles)),
    }


def student_stats(qs: QuerySet[StudentProfile]) -> dict:
    """Snapshot counts over an already-scoped student queryset (F2-4).

    Three aggregate queries total — counts, by-status, by-branch — so it stays
    cheap regardless of student count.
    """
    total = qs.count()
    with_cohort = qs.filter(current_cohort__isnull=False).count()
    blocked = qs.filter(blocked_at__isnull=False).count()
    by_status = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))}
    by_branch = {
        row["branch__name"]: row["n"]
        for row in qs.values("branch__name").annotate(n=Count("id")).order_by("-n")
    }
    return {
        "total": total,
        "with_cohort": with_cohort,
        "without_cohort": total - with_cohort,
        "blocked": blocked,
        "by_status": by_status,
        "by_branch": by_branch,
    }


def _unit_delta(unit: str):
    return {
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
        "week": timedelta(weeks=1),
        "month": relativedelta(months=1),
        "year": relativedelta(years=1),
    }[unit]


def student_comparison(qs: QuerySet[StudentProfile], *, metric: str, unit: str) -> dict:
    """Compare a metric this period vs the previous one (F2-5).

    metric="joined" counts new student records (StudentProfile.created_at);
    metric="left" counts withdrawn/graduated transitions (EnrollmentEvent). Both
    timestamps are datetimes, so unit="hour" is meaningful. `qs` is the caller's
    role-scoped student queryset (the comparison respects visibility).
    """
    now = timezone.now()
    delta = _unit_delta(unit)
    cur_start = now - delta
    prev_start = cur_start - delta

    if metric == "left":
        events = EnrollmentEvent.objects.filter(student__in=qs, to_status__in=_LEFT_STATUSES)
        current = events.filter(created_at__gte=cur_start, created_at__lt=now).count()
        previous = events.filter(created_at__gte=prev_start, created_at__lt=cur_start).count()
    else:  # joined
        current = qs.filter(created_at__gte=cur_start, created_at__lt=now).count()
        previous = qs.filter(created_at__gte=prev_start, created_at__lt=cur_start).count()

    delta_n = current - previous
    pct = round((delta_n / previous) * 100, 1) if previous else None
    return {
        "metric": metric,
        "unit": unit,
        "current": current,
        "previous": previous,
        "delta": delta_n,
        "pct_change": pct,
        "current_window": [cur_start.isoformat(), now.isoformat()],
        "previous_window": [prev_start.isoformat(), cur_start.isoformat()],
    }
