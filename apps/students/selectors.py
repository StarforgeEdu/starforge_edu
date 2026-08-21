"""Student read selectors with role-based scoping (TD-5)."""

from __future__ import annotations

from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.db.models import Count, ExpressionWrapper, F, IntegerField, Q, QuerySet
from django.db.models.functions import ExtractDay, ExtractMonth
from django.utils import timezone

from apps.students.models import EnrollmentEvent, StudentProfile
from core.permissions import PermissionRoleSet, Role, get_unambiguous_user_roles
from core.scoping import (
    permission_membership_is_unscoped,
    permission_membership_scope_q,
    permission_membership_scopes,
    role_membership_scope_q,
)

# What counts as "leaving" the center (for joined/left analytics).
_LEFT_STATUSES = (StudentProfile.Status.WITHDRAWN, StudentProfile.Status.GRADUATED)
_COMPARISON_UNITS = ("hour", "day", "week", "month", "year")

# Only the center director is tenant-wide. Every other staff role is attached to
# one or more RoleMembership branches and must stay inside those branches.  In
# particular, a head of department is a branch/department manager, not a second
# tenant-wide director.
TENANT_WIDE_ROLES = {Role.DIRECTOR}
BRANCH_STAFF_ROLES = {Role.HEAD_OF_DEPT, Role.TEACHER, Role.REGISTRAR, Role.IT}


def _base_qs() -> QuerySet[StudentProfile]:
    return StudentProfile.objects.select_related("user", "branch", "current_cohort").defer(
        "medical_notes",
        "emergency_contacts",
    )


def _teacher_owned_students(
    qs: QuerySet[StudentProfile], *, user, roles: set[str]
) -> QuerySet[StudentProfile]:
    """Keep an exact teacher principal inside cohorts they actually teach.

    A branch membership supplies the permission, but it is not a classroom
    ownership boundary. Only an explicit main/co-teacher assignment owns a
    classroom. A scheduled lesson may be cover or historical evidence and must
    never widen the teacher's student directory.
    """
    if not isinstance(roles, PermissionRoleSet) or roles.account_kinds != frozenset({"teacher"}):
        return qs

    from apps.cohorts.selectors import taught_cohorts

    return qs.filter(
        current_cohort_id__in=taught_cohorts(
            user_id=user.pk,
            include_lesson_teacher=False,
        ).values("pk")
    )


def scoped_students(*, user, roles: set[str] | None = None) -> QuerySet[StudentProfile]:
    qs = _base_qs()
    if user.is_superuser:
        return qs
    if roles is None:
        roles = get_unambiguous_user_roles(user)
    if isinstance(roles, PermissionRoleSet):
        if permission_membership_is_unscoped(
            roles=roles,
            permission="students:read",
            account_kinds={"staff", "teacher"},
        ):
            return _teacher_owned_students(qs, user=user, roles=roles)
    elif roles & TENANT_WIDE_ROLES:
        return qs

    visible = Q(pk__in=[])
    scoped_staff = roles & BRANCH_STAFF_ROLES
    if scoped_staff and not isinstance(roles, PermissionRoleSet):
        visible |= role_membership_scope_q(
            user=user,
            roles=scoped_staff,
            branch_field="branch_id",
            department_field="current_cohort__department_id",
        )
    # Canonical custom account types do not need a hard-coded legacy role to
    # become useful. Scope each grant to the exact membership that supplied it.
    visible |= permission_membership_scope_q(
        roles=roles,
        permission="students:read",
        branch_field="branch_id",
        department_field="current_cohort__department_id",
        account_kinds={"staff", "teacher"},
    )
    parent_reader = Role.PARENT in roles
    student_reader = Role.STUDENT in roles
    if isinstance(roles, PermissionRoleSet):
        parent_reader = bool(
            permission_membership_scopes(
                roles=roles,
                permission="students:read",
                account_kinds={"parent"},
            )
        )
        student_reader = bool(
            permission_membership_scopes(
                roles=roles,
                permission="students:read",
                account_kinds={"student"},
            )
        )
    if parent_reader:  # read_own_children
        visible |= Q(
            guardians__parent__user=user,
            guardians__revoked_at__isnull=True,
        )
    if student_reader:  # read_self
        visible |= Q(user=user)
    return _teacher_owned_students(qs.filter(visible).distinct(), user=user, roles=roles)


def students_with_upcoming_birthdays(
    *, base: QuerySet[StudentProfile] | None = None, days: int = 7, branch=None, cohort=None
) -> QuerySet[StudentProfile]:
    today = timezone.localdate()
    # Clamp defensively: the (month, day) set is exhaustive at 366 days anyway,
    # so capping is semantically lossless and protects future callers.
    month_days = {
        (today + timedelta(days=offset)).timetuple()[1:3] for offset in range(min(max(days, 0), 366) + 1)
    }
    # One compact ``IN`` expression instead of up to 366 OR branches (730
    # EXTRACT calls and ~50KB SQL for a year-wide request).
    month_day_values = [month * 100 + day for month, day in month_days]
    qs = (
        (base if base is not None else _base_qs())
        .filter(birthdate__isnull=False)
        .annotate(
            _birth_month_day=ExpressionWrapper(
                ExtractMonth(F("birthdate")) * 100 + ExtractDay(F("birthdate")),
                output_field=IntegerField(),
            )
        )
        .filter(_birth_month_day__in=month_day_values)
    )
    if branch:
        qs = qs.filter(branch_id=branch)
    if cohort:
        qs = qs.filter(current_cohort_id=cohort)
    return qs


def student_profile_for(user) -> StudentProfile | None:
    return (
        StudentProfile.objects.select_related("current_cohort")
        .defer("medical_notes", "emergency_contacts")
        .filter(user=user)
        .first()
    )


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
    # Clear StudentProfile's default ``-created_at`` ordering before grouped
    # aggregates. On a role-union queryset that legitimately needs DISTINCT
    # (parent guardianship joins), PostgreSQL otherwise adds created_at to the
    # SELECT/GROUP BY and splits one status into one-row buckets.
    grouped = qs.order_by()
    by_status = {row["status"]: row["n"] for row in grouped.values("status").annotate(n=Count("id"))}
    by_branch = {
        row["branch__name"]: row["n"]
        for row in grouped.values("branch__name").annotate(n=Count("id")).order_by("-n")
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
