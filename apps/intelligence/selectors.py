"""A-3 intelligence pipeline — student dropout-risk flags from TRANSPARENT RULES.

Dropout is the #1 revenue leak, so the first slice of the pipeline surfaces
at-risk students. There is deliberately NO black-box model: every flag is a
documented rule over data the center already has (attendance, published grades,
overdue invoices), computed on read so it is always current and fully explainable.
`RULES` is exposed verbatim through the API so a center sees exactly how flags fire.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import (
    Avg,
    Case,
    Count,
    ExpressionWrapper,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone

from apps.academics.models import ExamResult
from apps.attendance.models import AttendanceRecord
from apps.finance.models import Expense, Invoice, PaymentAllocation, Refund
from apps.intelligence.dto import ExecutiveScopeBoundary, ExecutiveSummaryContext
from apps.intelligence.executive import EXECUTIVE_SECTION_REQUIREMENTS
from apps.parents.models import Guardian
from apps.payments.models import Payment
from apps.schedule.models import Lesson
from apps.students.models import StudentProfile
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES

# --- transparent, documented thresholds (will move to CenterSettings later) ----- #
ATTENDANCE_WINDOW_DAYS = 30
MIN_LESSONS_FOR_ATTENDANCE_FLAG = 4
ABSENCE_RATE_THRESHOLD = 0.30  # absent >= 30% of recent lessons
LOW_GRADE_PCT_THRESHOLD = 50.0  # average published score < 50%

# Each rule's weight; the sum is the risk score, which maps to a level below.
RULES: dict[str, dict[str, Any]] = {
    "low_attendance": {
        "weight": 3,
        "description": (
            f"Absent in {int(ABSENCE_RATE_THRESHOLD * 100)}%+ of the last "
            f"{ATTENDANCE_WINDOW_DAYS} days' lessons "
            f"(min {MIN_LESSONS_FOR_ATTENDANCE_FLAG} lessons)."
        ),
    },
    "low_grades": {
        "weight": 2,
        "description": f"Average published exam score below {int(LOW_GRADE_PCT_THRESHOLD)}%.",
    },
    "overdue_payment": {"weight": 2, "description": "Has at least one overdue invoice."},
}


def _level(score: int) -> str:
    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    return "low"  # only reached for an at-risk student (score >= 1)


def student_risk(students: QuerySet[StudentProfile], *, now=None, include_finance: bool = True) -> list[dict]:
    """Compute risk flags for an already-scoped student queryset. Returns ONLY the
    at-risk students (>=1 flag), highest score first. A few aggregate queries (not
    one-per-student) keep it cheap. `include_finance=False` omits the overdue-payment
    flag for callers who may not see finance."""
    now = now or timezone.now()
    student_scope = students.order_by().values("id")
    ids = list(students.values_list("id", flat=True))
    if not ids:
        return []

    window = now - timedelta(days=ATTENDANCE_WINDOW_DAYS)
    attendance = {
        row["student_id"]: row
        # Window keys on the LESSON's date, not the row-write time, so a late
        # backfill/correction can't inject old lessons into "the last 30 days".
        # `total` excludes EXCUSED so an excused absence neither hurts nor dilutes.
        for row in AttendanceRecord.objects.filter(
            student_id__in=Subquery(student_scope),
            lesson__starts_at__gte=window,
            lesson__starts_at__lte=now,
        )
        .values("student_id")
        .annotate(
            total=Count("id", filter=~Q(status=AttendanceRecord.Status.EXCUSED)),
            absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
        )
    }
    grades = {
        row["student_id"]: row["avg_pct"]
        for row in ExamResult.objects.filter(student_id__in=Subquery(student_scope), exam__is_published=True)
        .values("student_id")
        .annotate(
            avg_pct=Avg(
                ExpressionWrapper(F("score") * 100.0 / F("exam__max_score"), output_field=FloatField())
            )
        )
    }
    # The overdue (financial) signal is only computed for callers who may see finance
    # — never leak a student's tuition-arrears status to a role without finance:read.
    overdue: set[int] = set()
    if include_finance:
        overdue = set(
            Invoice.objects.filter(
                student_id__in=Subquery(student_scope),
                status=Invoice.Status.OVERDUE,
                attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
                branch_at_issue_id=F("student__branch_id"),
            ).values_list("student_id", flat=True)
        )

    flagged: list[tuple[int, list[dict]]] = []
    for sid in ids:
        flags = _flags_for(attendance.get(sid), grades.get(sid), sid in overdue)
        if flags:
            flagged.append((sid, flags))

    # Load full rows (name/cohort) only for the flagged subset, not every scoped student.
    by_id = {
        s.id: s
        for s in StudentProfile.objects.filter(id__in=[sid for sid, _ in flagged]).select_related("user")
    }
    out: list[dict] = []
    for sid, flags in flagged:
        score = sum(RULES[f["code"]]["weight"] for f in flags)
        # The first id scan and this reload are separate READ COMMITTED queries. A
        # concurrent offboarding may legitimately remove the row between them.
        student = by_id.get(sid)
        if student is None:
            continue
        out.append(
            {
                "student": sid,
                "name": student.get_full_name(),
                "cohort": student.current_cohort_id,
                "score": score,
                "level": _level(score),
                "flags": flags,
            }
        )
    out.sort(key=lambda r: (-r["score"], r["student"]))
    return out


def _flags_for(att, avg_pct, is_overdue) -> list[dict]:
    flags: list[dict] = []
    if _is_low_attendance(att):
        flags.append(
            {"code": "low_attendance", "reason": f"Absent {att['absent']} of last {att['total']} lessons."}
        )
    if avg_pct is not None and avg_pct < LOW_GRADE_PCT_THRESHOLD:
        flags.append({"code": "low_grades", "reason": f"Recent average {round(avg_pct, 1)}%."})
    if is_overdue:
        flags.append({"code": "overdue_payment", "reason": "Has an overdue invoice."})
    return flags


def _is_low_attendance(attendance: dict[str, int] | None) -> bool:
    return bool(
        attendance
        and attendance["total"] >= MIN_LESSONS_FOR_ATTENDANCE_FLAG
        and (attendance["absent"] / attendance["total"]) >= ABSENCE_RATE_THRESHOLD
    )


def _risk_signal_data(
    *,
    students: QuerySet[StudentProfile],
    attendance_records: QuerySet[AttendanceRecord],
    exam_results: QuerySet[ExamResult],
    overdue_invoices: QuerySet[Invoice] | None,
) -> tuple[dict[int, dict[str, int]], dict[int, float], set[int]]:
    """Read each risk signal once for an already-authorized student scope.

    Keeping the scope as a subquery lets PostgreSQL use one semi-join per signal.
    In particular, it avoids Django expanding references to correlated annotation
    aliases every time the score, filter, ordering, or aggregate mentions them.
    """

    student_ids = students.order_by().values("pk")
    attendance = {
        int(row["student_id"]): {
            "total": int(row["total"]),
            "absent": int(row["absent"]),
        }
        for row in attendance_records.filter(student_id__in=Subquery(student_ids))
        .order_by()
        .values("student_id")
        .annotate(
            total=Count("id", filter=~Q(status=AttendanceRecord.Status.EXCUSED)),
            absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
        )
    }
    grades = {
        int(row["student_id"]): float(row["avg_pct"])
        for row in exam_results.filter(student_id__in=Subquery(student_ids))
        .order_by()
        .values("student_id")
        .annotate(
            avg_pct=Avg(
                ExpressionWrapper(
                    F("score") * 100.0 / F("exam__max_score"),
                    output_field=FloatField(),
                )
            )
        )
        if row["avg_pct"] is not None
    }
    overdue: set[int] = set()
    if overdue_invoices is not None:
        overdue = set(
            overdue_invoices.filter(student_id__in=Subquery(student_ids))
            .order_by()
            .values_list("student_id", flat=True)
            .distinct()
        )
    return attendance, grades, overdue


def _risk_flags_by_student(
    attendance: dict[int, dict[str, int]],
    grades: dict[int, float],
    overdue: set[int],
) -> dict[int, list[dict]]:
    candidates = set(attendance) | set(grades) | overdue
    return {
        student_id: flags
        for student_id in candidates
        if (flags := _flags_for(attendance.get(student_id), grades.get(student_id), student_id in overdue))
    }


def student_risk_page(
    students: QuerySet[StudentProfile],
    *,
    include_finance: bool,
    page: int,
    page_size: int,
    now=None,
) -> tuple[list[dict[str, Any]], int]:
    """Return a globally-ranked page from three set-based signal aggregates."""

    now = now or timezone.now()
    window = now - timedelta(days=ATTENDANCE_WINDOW_DAYS)
    overdue_invoices = None
    if include_finance:
        overdue_invoices = Invoice.objects.filter(
            status=Invoice.Status.OVERDUE,
            attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
            branch_at_issue_id=F("student__branch_id"),
        )
    attendance, grades, overdue = _risk_signal_data(
        students=students,
        attendance_records=AttendanceRecord.objects.filter(
            lesson__starts_at__gte=window,
            lesson__starts_at__lte=now,
        ),
        exam_results=ExamResult.objects.filter(
            exam__is_published=True,
        ),
        overdue_invoices=overdue_invoices,
    )
    flags_by_student = _risk_flags_by_student(attendance, grades, overdue)
    ranked = [
        (
            sum(RULES[flag["code"]]["weight"] for flag in flags),
            student_id,
            flags,
        )
        for student_id, flags in flags_by_student.items()
    ]
    ranked.sort(key=lambda row: (-row[0], row[1]))
    total = len(ranked)
    offset = (page - 1) * page_size
    if offset > 1_000_000_000:
        return [], total
    page_rows = ranked[offset : offset + page_size]
    page_ids = [student_id for _score, student_id, _flags in page_rows]
    students_by_id = {
        student.pk: student
        for student in students.filter(pk__in=page_ids).select_related("user", "current_cohort")
    }
    results: list[dict[str, Any]] = []
    for score, student_id, flags in page_rows:
        student = students_by_id.get(student_id)
        if student is None:
            continue
        results.append(
            {
                "student": student_id,
                "name": student.get_full_name(),
                "cohort": student.current_cohort_id,
                "score": score,
                "level": _level(score),
                "flags": flags,
            }
        )
    return results, total


# --- A-3 facet: branch performance ranking --------------------------------------- #
# A transparent owner view: how each branch is doing across attendance, published
# grades, and dropout-risk, blended into one 0-100 score. Model-less / compute-on-read
# like the risk flags. The weights are documented and exposed verbatim via the API.
ACTIVE_STUDENT_STATUSES = (StudentProfile.Status.ENROLLED, StudentProfile.Status.ACTIVE)
BRANCH_WEIGHT_ATTENDANCE = 50  # show-up rate is the strongest health signal
BRANCH_WEIGHT_GRADES = 30
BRANCH_WEIGHT_LOW_RISK = 20  # the inverse of the at-risk share
# Small-cell suppression (k-anonymity): a branch with fewer than this many active
# students has its per-student-revealing metrics (and score) suppressed, so a "branch
# aggregate" can never round-trip one identifiable student's attendance/grade/risk.
MIN_BRANCH_CELL = 3

BRANCH_METRICS: dict[str, dict[str, Any]] = {
    "attendance_rate": {
        "weight": BRANCH_WEIGHT_ATTENDANCE,
        "description": "Share of recent non-excused marks that were present or late.",
    },
    "avg_grade_pct": {
        "weight": BRANCH_WEIGHT_GRADES,
        "description": "Average score across the branch's published exam results.",
    },
    "low_risk": {
        "weight": BRANCH_WEIGHT_LOW_RISK,
        "description": "1 minus the share of active students carrying a dropout-risk flag.",
    },
}


def _branch_score(attendance_rate, avg_grade_pct, at_risk_rate) -> float:
    """Blend the signals into 0-100. Called only for a branch that HAS an academic
    signal (attendance or grades), so a no-data branch is left unranked (None) by the
    caller rather than earning spurious risk credit. A raw score that overshoots (e.g.
    a bonus exam score above max) is clamped to the advertised 0-100 range."""
    att = attendance_rate if attendance_rate is not None else 0.0
    grade = (avg_grade_pct / 100.0) if avg_grade_pct is not None else 0.0
    low_risk = (1.0 - at_risk_rate) if at_risk_rate is not None else 1.0
    raw = att * BRANCH_WEIGHT_ATTENDANCE + grade * BRANCH_WEIGHT_GRADES + low_risk * BRANCH_WEIGHT_LOW_RISK
    return round(max(0.0, min(100.0, raw)), 1)


def branch_ranking(branches, *, now=None, include_finance: bool = True) -> list[dict]:
    """Rank an already-scoped Branch queryset by a transparent performance score over
    each branch's ACTIVE/ENROLLED students. A handful of grouped aggregates (not one
    query per branch) keep it cheap. `include_finance=False` omits the overdue count
    for callers without finance:read.

    Privacy: a branch with fewer than MIN_BRANCH_CELL active students has its metrics
    and score SUPPRESSED. Each academic metric also requires MIN_BRANCH_CELL distinct
    contributing students; a large branch with one graded student must not reveal that
    student's exact score as an "aggregate". A branch with no safely reportable academic
    signal is left unranked (score None). Unscored rows sort last."""
    now = now or timezone.now()
    branch_ids = list(branches.values_list("id", flat=True))
    if not branch_ids:
        return []
    window = now - timedelta(days=ATTENDANCE_WINDOW_DAYS)
    students = StudentProfile.objects.filter(branch_id__in=branch_ids, status__in=ACTIVE_STUDENT_STATUSES)

    active_by_branch = {
        row["branch_id"]: row["n"] for row in students.values("branch_id").annotate(n=Count("id"))
    }
    attendance = {
        row["student__branch_id"]: row
        for row in AttendanceRecord.objects.filter(
            student__branch_id__in=branch_ids,
            student__status__in=ACTIVE_STUDENT_STATUSES,
            lesson__starts_at__gte=window,
            lesson__starts_at__lte=now,
        )
        .values("student__branch_id")
        .annotate(
            total=Count("id", filter=~Q(status=AttendanceRecord.Status.EXCUSED)),
            attended=Count(
                "id",
                filter=Q(status__in=(AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE)),
            ),
            contributors=Count("student_id", distinct=True),
        )
    }
    grades = {
        row["student__branch_id"]: row
        for row in ExamResult.objects.filter(
            student__branch_id__in=branch_ids,
            student__status__in=ACTIVE_STUDENT_STATUSES,
            exam__is_published=True,
        )
        .values("student__branch_id")
        .annotate(
            avg_pct=Avg(
                ExpressionWrapper(F("score") * 100.0 / F("exam__max_score"), output_field=FloatField())
            ),
            contributors=Count("student_id", distinct=True),
        )
    }
    # At-risk count per branch: compute risk once over all active students, map to branch.
    risk_ids = {r["student"] for r in student_risk(students, now=now, include_finance=include_finance)}
    at_risk_by_branch: dict[int, int] = {}
    if risk_ids:
        for _sid, bid in StudentProfile.objects.filter(id__in=risk_ids).values_list("id", "branch_id"):
            at_risk_by_branch[bid] = at_risk_by_branch.get(bid, 0) + 1

    overdue_by_branch: dict[int, int] = {}
    if include_finance:
        overdue_by_branch = {
            row["student__branch_id"]: row["n"]
            for row in Invoice.objects.filter(
                student__branch_id__in=branch_ids,
                student__status__in=ACTIVE_STUDENT_STATUSES,
                status=Invoice.Status.OVERDUE,
            )
            .values("student__branch_id")
            .annotate(n=Count("student_id", distinct=True))
        }

    names = dict(branches.values_list("id", "name"))
    out: list[dict] = []
    for bid in branch_ids:
        active = active_by_branch.get(bid, 0)
        suppressed = 0 < active < MIN_BRANCH_CELL
        if suppressed:
            # Too few students to anonymise — expose only the headcount, nothing that
            # could round-trip an individual student's attendance/grade/risk.
            out.append(
                {
                    "branch": bid,
                    "name": names.get(bid, ""),
                    "active_students": active,
                    "attendance_rate": None,
                    "avg_grade_pct": None,
                    "at_risk": None,
                    "at_risk_rate": None,
                    "overdue_students": None,
                    "suppressed": True,
                    "score": None,
                }
            )
            continue
        att = attendance.get(bid)
        if att and att["contributors"] < MIN_BRANCH_CELL:
            att = None
        attendance_rate = (att["attended"] / att["total"]) if att and att["total"] else None
        grade = grades.get(bid)
        avg_grade = grade["avg_pct"] if grade and grade["contributors"] >= MIN_BRANCH_CELL else None
        at_risk = at_risk_by_branch.get(bid, 0)
        at_risk_rate = (at_risk / active) if active else None
        # Only score a branch that has an academic signal; a no-data branch stays
        # unranked rather than collecting a spurious low-risk credit.
        has_signal = attendance_rate is not None or avg_grade is not None
        out.append(
            {
                "branch": bid,
                "name": names.get(bid, ""),
                "active_students": active,
                "attendance_rate": round(attendance_rate, 3) if attendance_rate is not None else None,
                "avg_grade_pct": round(avg_grade, 1) if avg_grade is not None else None,
                "at_risk": at_risk if active else None,
                "at_risk_rate": round(at_risk_rate, 3) if at_risk_rate is not None else None,
                "overdue_students": overdue_by_branch.get(bid, 0) if include_finance else None,
                "suppressed": False,
                "score": _branch_score(attendance_rate, avg_grade, at_risk_rate) if has_signal else None,
            }
        )
    # Highest score first; unscored rows (empty / suppressed / no-signal) sort last.
    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0.0), r["branch"]))
    for rank, row in enumerate(out, start=1):
        row["rank"] = rank
    return out


# --- A-3 facet: family health (retention) ---------------------------------------- #
# A per-FAMILY view for the retention desk: which families have an at-risk or
# overdue child and so are worth a call before they leave. Deliberately NOT
# anonymised — the whole point is to name the family to follow up — so it is gated to
# roles that already see family records (parents:read) and the overdue signal is
# finance-gated. Transparent levels, like the risk rules.
FAMILY_HEALTH_LEVELS: dict[str, str] = {
    "at_risk": "An overdue child, or at least half the children carry a dropout-risk flag.",
    "watch": "At least one child carries a dropout-risk flag.",
    "good": "No dropout-risk flags and nothing overdue.",
}


def _family_health_level(children: int, at_risk: int, overdue: int | None) -> str:
    if (overdue or 0) > 0 or (children and at_risk / children >= 0.5):
        return "at_risk"
    if at_risk > 0:
        return "watch"
    return "good"


def family_health(branches, *, now=None, include_finance: bool = True) -> list[dict]:
    """Score each family (a guardian + the children they guard, within the scoped
    branches) for retention risk. Reuses the dropout-risk rules for the children and,
    when finance is visible, their overdue invoices. Worst-health families first."""
    now = now or timezone.now()
    branch_ids = list(branches.values_list("id", flat=True))
    if not branch_ids:
        return []
    students = StudentProfile.objects.filter(branch_id__in=branch_ids, status__in=ACTIVE_STUDENT_STATUSES)
    student_scope = students.order_by().values("id")
    if not students.exists():
        return []

    families: dict[int, dict] = {}
    for g in Guardian.objects.filter(
        student_id__in=Subquery(student_scope),
        revoked_at__isnull=True,
        parent__is_active=True,
        parent__user__is_active=True,
    ).select_related("parent__user"):
        parent_user = g.parent.user
        fam = families.setdefault(
            g.parent_id,
            {"name": parent_user.get_full_name() if parent_user else "", "children": set()},
        )
        fam["children"].add(g.student_id)
    if not families:
        return []

    at_risk_ids = {r["student"] for r in student_risk(students, now=now, include_finance=include_finance)}
    overdue_ids: set[int] = set()
    if include_finance:
        overdue_ids = set(
            Invoice.objects.filter(
                student_id__in=Subquery(student_scope), status=Invoice.Status.OVERDUE
            ).values_list("student_id", flat=True)
        )

    out: list[dict] = []
    for parent_id, fam in families.items():
        children = fam["children"]
        at_risk = len(children & at_risk_ids)
        overdue = len(children & overdue_ids) if include_finance else None
        out.append(
            {
                "family": parent_id,
                "name": fam["name"],
                "children": len(children),
                "at_risk_children": at_risk,
                "overdue_children": overdue,
                "health": _family_health_level(len(children), at_risk, overdue),
            }
        )
    order = {"at_risk": 0, "watch": 1, "good": 2}
    out.sort(key=lambda r: (order.get(r["health"], 9), -r["at_risk_children"], r["family"]))
    return out


# --- A-3 facet: student journey timeline ------------------------------------------ #
# One student's story in one chronological feed — enrollment moves, published grades,
# achievements, and (finance-gated) invoices — so the family and staff can see the
# whole journey at a glance instead of digging through five screens (paper-elimination
# / dignity DNA). Compute-on-read; the invoice events are omitted unless the caller may
# see finance (the view passes include_finance=False for everyone but finance + the
# student/guardian themselves).
def student_journey(student: StudentProfile, *, include_finance: bool = True, limit: int = 100) -> list[dict]:
    from apps.achievements.models import AchievementGrant

    events: list[dict] = []

    for ev in student.enrollment_events.all():
        events.append(
            {
                "at": ev.created_at,
                "type": "enrollment",
                "title": f"{ev.from_status or 'new'} → {ev.to_status}",
                "detail": ev.reason_code or ev.note[:140],
            }
        )
    for r in ExamResult.objects.filter(student=student, exam__is_published=True).select_related(
        "exam__subject"
    ):
        max_score = r.exam.max_score
        pct = round(float(r.score) * 100.0 / float(max_score), 1) if max_score else None
        detail = f"{r.score}/{max_score}" + (f" ({pct}%)" if pct is not None else "")
        events.append({"at": r.graded_at, "type": "grade", "title": r.exam.subject.name, "detail": detail})
    for g in AchievementGrant.objects.filter(student=student).select_related("achievement"):
        events.append(
            {"at": g.granted_at, "type": "achievement", "title": g.achievement.name, "detail": g.note}
        )
    if include_finance:
        for inv in Invoice.objects.filter(student=student):
            events.append(
                {
                    "at": inv.created_at,
                    "type": "invoice",
                    "title": f"Invoice {inv.number}",
                    "detail": f"{inv.total_uzs} UZS — {inv.status}",
                }
            )

    events.sort(key=lambda e: e["at"], reverse=True)
    events = events[:limit]
    for e in events:
        e["at"] = e["at"].isoformat()  # serialise the datetime for the API
    return events


def student_risk_detail(student: StudentProfile, *, now=None, include_finance: bool = True) -> dict:
    """Full risk picture for ONE student (transparency view) — the flags it fires
    plus a 'none' result when it's healthy, so a center can always see the reasoning."""
    rows = student_risk(
        StudentProfile.objects.filter(pk=student.pk), now=now, include_finance=include_finance
    )
    if rows:
        return rows[0]
    return {
        "student": student.pk,
        "name": student.get_full_name(),
        "cohort": student.current_cohort_id,
        "score": 0,
        "level": "none",
        "flags": [],
    }


# --- A-3 teacher engagement facet ---------------------------------------------- #
# HONEST FRAMING: this measures ENGAGEMENT (do students show up to this teacher's
# lessons) + REACH, NOT causal "value-add" (which needs controlled pre/post data we
# don't have). It is a transparent rule over attendance the centre already records,
# attributed cleanly by Lesson.teacher. Grades are deliberately NOT attributed to a
# teacher (a cohort's outcome has many inputs). Per-teacher named, so the VIEW gates
# it to managers + a teacher's own row (dignity: no public teacher leaderboard).

TEACHER_METRICS: dict[str, str] = {
    "attendance_rate": "Share of recent non-excused marks in this teacher's lessons that were present or late.",
    "lessons_delivered": "Count of the teacher's non-cancelled lessons in the window.",
    "students_reached": "Distinct students who had a mark in the teacher's lessons.",
}


def teacher_engagement_page(
    teachers: QuerySet,
    *,
    page: int,
    page_size: int,
    now=None,
) -> tuple[list[dict[str, Any]], int]:
    """Return the globally-ranked teacher page from bounded SQL.

    Correlated aggregates preserve the transparent metric definitions while
    applying ranking and pagination before teacher rows are materialized.
    """

    now = now or timezone.now()
    window = now - timedelta(days=ATTENDANCE_WINDOW_DAYS)
    status = AttendanceRecord.Status
    attendance_stats = (
        AttendanceRecord.objects.filter(
            lesson__teacher_id=OuterRef("pk"),
            lesson__starts_at__gte=window,
            lesson__starts_at__lte=now,
        )
        .values("lesson__teacher_id")
        .annotate(
            denominator=Count("id", filter=~Q(status=status.EXCUSED)),
            attended=Count("id", filter=Q(status__in=(status.PRESENT, status.LATE))),
            students=Count("student_id", distinct=True),
        )
    )
    lesson_stats = (
        Lesson.objects.filter(
            teacher_id=OuterRef("pk"),
            starts_at__gte=window,
            starts_at__lte=now,
        )
        .exclude(status__in=(Lesson.Status.CANCELLED, Lesson.Status.ARCHIVED))
        .values("teacher_id")
        .annotate(total=Count("id"))
    )
    ranked = (
        teachers.order_by()
        .annotate(
            engagement_marks_sampled=Coalesce(
                Subquery(attendance_stats.values("denominator")[:1]),
                Value(0),
                output_field=IntegerField(),
            ),
            engagement_attended=Coalesce(
                Subquery(attendance_stats.values("attended")[:1]),
                Value(0),
                output_field=IntegerField(),
            ),
            engagement_students_reached=Coalesce(
                Subquery(attendance_stats.values("students")[:1]),
                Value(0),
                output_field=IntegerField(),
            ),
            engagement_lessons_delivered=Coalesce(
                Subquery(lesson_stats.values("total")[:1]),
                Value(0),
                output_field=IntegerField(),
            ),
        )
        .annotate(
            engagement_rate=Case(
                When(
                    engagement_marks_sampled__gt=0,
                    then=ExpressionWrapper(
                        Cast(F("engagement_attended"), FloatField())
                        * Value(100.0)
                        / Cast(F("engagement_marks_sampled"), FloatField()),
                        output_field=FloatField(),
                    ),
                ),
                default=Value(None),
                output_field=FloatField(),
            )
        )
        .order_by(F("engagement_rate").desc(nulls_last=True), "pk")
    )
    total = ranked.count()
    offset = (page - 1) * page_size
    if offset > 1_000_000_000:
        return [], total
    rows = list(ranked.select_related("user")[offset : offset + page_size])
    results: list[dict[str, Any]] = []
    for teacher in rows:
        rate = round(teacher.engagement_rate, 1) if teacher.engagement_rate is not None else None
        results.append(
            {
                "teacher": teacher.pk,
                "name": teacher.get_full_name(),
                "lessons_delivered": teacher.engagement_lessons_delivered,
                "students_reached": teacher.engagement_students_reached,
                "marks_sampled": teacher.engagement_marks_sampled,
                "attendance_rate": rate,
                "engagement_score": rate,
            }
        )
    return results, total


# --- Permission-pruned executive snapshot ------------------------------------ #

_OPEN_INVOICE_STATUSES = (
    Invoice.Status.ISSUED,
    Invoice.Status.PARTIALLY_PAID,
    Invoice.Status.OVERDUE,
)
_ZERO = Decimal("0")


def executive_summary(context: ExecutiveSummaryContext) -> dict[str, Any]:
    """Build one bounded, permission-pruned management snapshot.

    Every metric is a database aggregate or a branch-grouped aggregate.  No
    student, attendance, invoice, or payment population is materialized in
    Python.  ``context.scope`` was already authorized by the view, while
    ``included_sections`` records which domain permissions cover that *entire*
    scope; an unauthorized section is omitted instead of rendered as zero.
    """

    included = context.included_sections
    coverage: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    for section, alternatives in EXECUTIVE_SECTION_REQUIREMENTS.items():
        requirement = _coverage_requirement(alternatives)
        if section in included:
            coverage[section] = {"status": "complete", **requirement}
        else:
            coverage[section] = {
                "status": "omitted",
                "reason": "insufficient_permission",
                **requirement,
            }

    payload: dict[str, Any] = {
        "generated_at": context.generated_at.isoformat(),
        "locale": context.locale,
        "currency": context.currency,
        "window": context.window.to_dict(),
        "scope": context.scope.to_dict(),
        "coverage": coverage,
        "warnings": warnings,
    }

    student_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="branch_id",
        department_field="current_cohort__department_id",
    )
    attendance_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="lesson__cohort__branch_id",
        department_field="lesson__cohort__department_id",
    )
    lower, upper = _window_bounds(context)

    branch_metrics: dict[int, dict[str, Any]] = {
        branch.id: {"id": branch.id, "name": branch.name} for branch in context.scope.branches
    }

    if "students" in included:
        students = StudentProfile.objects.filter(student_scope).order_by()
        student_totals = students.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status=StudentProfile.Status.ACTIVE)),
            leads=Count("id", filter=Q(status=StudentProfile.Status.LEAD)),
            graduated=Count("id", filter=Q(status=StudentProfile.Status.GRADUATED)),
            withdrawn=Count("id", filter=Q(status=StudentProfile.Status.WITHDRAWN)),
            blocked=Count("id", filter=Q(blocked_at__isnull=False)),
            with_cohort=Count("id", filter=Q(current_cohort__isnull=False)),
            ungrouped=Count("id", filter=Q(current_cohort__isnull=True)),
            joined_in_window=Count(
                "id",
                filter=Q(
                    enrollment_date__gte=context.window.date_from,
                    enrollment_date__lte=context.window.date_to,
                ),
            ),
        )
        payload["students"] = student_totals
        coverage["students"]["sample_size"] = student_totals["total"]
        coverage["students"]["windowed_metrics"] = ["joined_in_window"]
        coverage["students"]["as_of_generated_at"] = [
            "total",
            "active",
            "leads",
            "graduated",
            "withdrawn",
            "blocked",
            "with_cohort",
            "ungrouped",
        ]
        for row in students.values("branch_id").annotate(student_count=Count("id")):
            if row["branch_id"] in branch_metrics:
                branch_metrics[row["branch_id"]]["student_count"] = row["student_count"]
        for row in branch_metrics.values():
            row.setdefault("student_count", 0)

    if "retention" in included:
        payload["retention"] = _retention_summary(
            context,
            student_scope=student_scope,
            lower=lower,
            upper=upper,
        )
        coverage["retention"].update(
            {
                "sample_size": payload["retention"]["current_student_sample_size"],
                "metric_definition": (
                    "Distinct currently scoped students with enrollment or terminal "
                    "transition evidence in the selected window."
                ),
                "attribution": "current_student_scope",
            }
        )

    if "capacity" in included:
        payload["capacity"] = _capacity_summary(context)
        coverage["capacity"].update(
            {
                "sample_size": payload["capacity"]["active_group_count"],
                "metric_definition": (
                    "Current active students divided by declared seats in active groups; "
                    "groups without capacity remain explicitly unmeasured."
                ),
            }
        )

    if "risk" in included:
        payload["risk"] = _risk_summary(
            context,
            student_scope=student_scope,
            lower=lower,
            upper=upper,
            include_finance="finance" in included,
        )
        coverage["risk"].update(
            {
                "sample_size": payload["risk"]["student_sample_size"],
                "signals": payload["risk"]["included_signals"],
                "metric_definition": (
                    "Transparent attendance, published-assessment, and (when authorized) "
                    "immutable-scope overdue-invoice rules; this is not a predictive model."
                ),
            }
        )

    if "attendance" in included:
        attendance = AttendanceRecord.objects.filter(
            attendance_scope,
            lesson__starts_at__gte=lower,
            lesson__starts_at__lt=upper,
        ).order_by()
        attendance_totals = attendance.aggregate(
            attended=Count(
                "id",
                filter=Q(status__in=(AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE)),
            ),
            absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
            excused=Count("id", filter=Q(status=AttendanceRecord.Status.EXCUSED)),
            denominator=Count("id", filter=~Q(status=AttendanceRecord.Status.EXCUSED)),
        )
        denominator = attendance_totals["denominator"]
        attendance_totals["attendance_rate_fraction"] = (
            round(attendance_totals["attended"] / denominator, 4) if denominator else None
        )
        payload["attendance"] = attendance_totals
        coverage["attendance"]["sample_size"] = denominator
        coverage["attendance"]["rate_definition"] = "present_or_late divided by non_excused marks"
        if not denominator:
            coverage["attendance"]["status"] = "no_data"
            warnings.append(
                {
                    "code": "insufficient_data",
                    "message": "Attendance has no eligible records in the selected window.",
                    "affected_sections": ["attendance"],
                }
            )
        for row in attendance.values("lesson__cohort__branch_id").annotate(
            attended=Count(
                "id",
                filter=Q(status__in=(AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE)),
            ),
            denominator=Count("id", filter=~Q(status=AttendanceRecord.Status.EXCUSED)),
        ):
            branch_id = row["lesson__cohort__branch_id"]
            if branch_id not in branch_metrics:
                continue
            branch_metrics[branch_id].update(
                {
                    "attendance_numerator": row["attended"],
                    "attendance_denominator": row["denominator"],
                    "attendance_rate_fraction": (
                        round(row["attended"] / row["denominator"], 4) if row["denominator"] else None
                    ),
                }
            )
        for row in branch_metrics.values():
            row.setdefault("attendance_numerator", 0)
            row.setdefault("attendance_denominator", 0)
            row.setdefault("attendance_rate_fraction", None)

    if "students" in included or "attendance" in included:
        payload["branches"] = [branch_metrics[branch.id] for branch in context.scope.branches]
        coverage["branches"] = {
            "status": "complete",
            "derived_from": [section for section in ("students", "attendance") if section in included],
        }

    if "finance" in included:
        payload["finance"] = _finance_summary(context, lower=lower, upper=upper)
        coverage["finance"]["currency"] = context.currency
        coverage["finance"]["window_basis"] = {
            "billed": "invoice issue_date",
            "collected": "payment paid_at",
            "refunded": "provider_confirmed_at",
            "expenses": "approved_at or paid_at",
        }
        coverage["finance"]["attribution"] = "immutable_historical_scope"
        if any(boundary.department_id is not None for boundary in context.scope.boundaries):
            # Expense has immutable branch attribution but no department column.
            # Returning whole-branch expenses for a department scope would broaden
            # authority, so only those two metrics are omitted and explained.
            coverage["finance"]["status"] = "partial"
            coverage["finance"]["omitted_metrics"] = ["approved_expense", "paid_expense"]
            warnings.append(
                {
                    "code": "scope_not_representable",
                    "message": "Expense totals are unavailable for department-only scope.",
                    "affected_sections": ["finance"],
                }
            )

    if "teachers" in included:
        payload["teachers"] = _teacher_summary(
            context,
            lower=lower,
            upper=upper,
        )
        coverage["teachers"].update(
            {
                "sample_size": payload["teachers"]["teacher_count"],
                "metric_definition": (
                    "Delivery, reach, attendance, group-load, and published-assessment "
                    "evidence in the selected window; no causal employee score is inferred."
                ),
            }
        )

    attention: dict[str, Any] = {}
    if "tasks" in included:
        attention["tasks"] = _task_attention(context)
        coverage["tasks"].update(
            {
                "sample_size": attention["tasks"]["open_assigned_to_me"],
                "attribution": "exact_assignee_principal",
            }
        )
    if "approvals" in included:
        if _branch_only_scope(context):
            attention["pending_approvals"] = _pending_approval_count(
                context,
                include_compensation="_compensation" in included,
            )
            coverage["approvals"].update(
                {
                    "sample_size": attention["pending_approvals"],
                    "attribution": "handler_branch_scope",
                }
            )
        else:
            _mark_scope_unrepresentable(
                coverage,
                warnings,
                section="approvals",
                message="Approval totals are unavailable for department-only scope.",
            )
    if "notifications" in included:
        # Notification rows intentionally contain no mutable resource-derived
        # branch relation. Return an exact personal total only for a tenant-wide
        # snapshot; a branch-filtered total would silently mix scopes.
        if context.scope.organization_wide:
            attention["unread_notifications"] = _unread_notification_count(context)
            coverage["notifications"].update(
                {
                    "sample_size": attention["unread_notifications"],
                    "attribution": "exact_recipient_principal",
                }
            )
        else:
            _mark_scope_unrepresentable(
                coverage,
                warnings,
                section="notifications",
                message="Unread notifications are unavailable for a filtered organization scope.",
            )
    if "meetings" in included:
        if _branch_only_scope(context):
            attention["upcoming_meetings"] = _upcoming_meeting_count(
                context,
                upper=upper,
            )
            coverage["meetings"].update(
                {
                    "sample_size": attention["upcoming_meetings"],
                    "attribution": "exact_invitee_principal",
                }
            )
        else:
            _mark_scope_unrepresentable(
                coverage,
                warnings,
                section="meetings",
                message="Upcoming meetings are unavailable for department-only scope.",
            )
    if attention:
        payload["attention"] = attention

    permission_omissions = [
        section
        for section, item in coverage.items()
        if item["status"] == "omitted" and item.get("reason") == "insufficient_permission"
    ]
    if permission_omissions:
        warnings.append(
            {
                "code": "sections_omitted",
                "message": "Some sections were omitted because they are not authorized.",
                "affected_sections": permission_omissions,
            }
        )
    return payload


def _coverage_requirement(
    alternatives: tuple[tuple[str, ...], ...],
) -> dict[str, Any]:
    if alternatives == ((),):
        return {"authorization_basis": "current_principal"}
    if len(alternatives) == 1 and len(alternatives[0]) == 1:
        return {"required_permission": alternatives[0][0]}
    return {"required_permission_sets": [list(all_of) for all_of in alternatives]}


def _scope_predicate(
    boundaries: tuple[ExecutiveScopeBoundary, ...],
    *,
    branch_field: str,
    department_field: str,
) -> Q:
    predicate = Q(pk__in=[])
    for boundary in boundaries:
        if boundary.department_id is None:
            predicate |= Q(**{branch_field: boundary.branch_id})
        else:
            predicate |= Q(
                **{
                    branch_field: boundary.branch_id,
                    department_field: boundary.department_id,
                }
            )
    return predicate


def _window_bounds(context: ExecutiveSummaryContext) -> tuple[datetime, datetime]:
    tz = ZoneInfo(context.window.timezone)
    lower = timezone.make_aware(datetime.combine(context.window.date_from, time.min), tz)
    # Exclusive next-day bound is precise across database timestamp precision and
    # avoids manufacturing a 23:59:59.999999 sentinel.
    upper = timezone.make_aware(
        datetime.combine(context.window.date_to + timedelta(days=1), time.min),
        tz,
    )
    return lower, upper


def _retention_summary(
    context: ExecutiveSummaryContext,
    *,
    student_scope: Q,
    lower: datetime,
    upper: datetime,
) -> dict[str, int | str]:
    from apps.students.models import EnrollmentEvent

    scoped_students = StudentProfile.objects.filter(student_scope).order_by()
    transitions = EnrollmentEvent.objects.filter(
        student_id__in=Subquery(scoped_students.values("pk")),
        created_at__gte=lower,
        created_at__lt=upper,
    ).order_by()
    totals = transitions.aggregate(
        exit_events=Count(
            "id",
            filter=Q(
                to_status__in=(
                    StudentProfile.Status.GRADUATED,
                    StudentProfile.Status.WITHDRAWN,
                )
            ),
        ),
        exited_students=Count(
            "student_id",
            filter=Q(
                to_status__in=(
                    StudentProfile.Status.GRADUATED,
                    StudentProfile.Status.WITHDRAWN,
                )
            ),
            distinct=True,
        ),
    )
    student_totals = scoped_students.aggregate(
        current_student_sample_size=Count("id"),
        joined_students=Count(
            "id",
            filter=Q(
                enrollment_date__gte=context.window.date_from,
                enrollment_date__lte=context.window.date_to,
            ),
        ),
    )
    return {
        "current_student_sample_size": student_totals["current_student_sample_size"],
        "joined_students": student_totals["joined_students"],
        "exited_students": totals["exited_students"],
        "exit_events": totals["exit_events"],
        "attribution": "current_student_scope",
    }


def _capacity_summary(context: ExecutiveSummaryContext) -> dict[str, int | str]:
    from apps.cohorts.models import Cohort

    scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="branch_id",
        department_field="department_id",
    )
    cohorts = Cohort.objects.filter(scope, is_archived=False).order_by()
    # Keep cohort aggregates independent from the reverse student join. Mixing
    # them into one aggregate multiplies every cohort (and its capacity) by its
    # current-student count.
    group_totals = cohorts.aggregate(
        active_group_count=Count("id"),
        groups_with_declared_capacity=Count("id", filter=Q(capacity__isnull=False)),
        groups_without_declared_capacity=Count("id", filter=Q(capacity__isnull=True)),
        declared_seats=Sum("capacity"),
    )
    student_totals = StudentProfile.objects.filter(
        current_cohort__in=cohorts,
        status=StudentProfile.Status.ACTIVE,
    ).aggregate(
        active_students=Count(
            "id",
        ),
        active_students_in_measured_groups=Count(
            "id",
            filter=Q(current_cohort__capacity__isnull=False),
        ),
    )
    declared = group_totals["declared_seats"] or 0
    measured_students = student_totals["active_students_in_measured_groups"]
    return {
        "active_group_count": group_totals["active_group_count"],
        "groups_with_declared_capacity": group_totals["groups_with_declared_capacity"],
        "groups_without_declared_capacity": group_totals["groups_without_declared_capacity"],
        "declared_seats": declared,
        "active_students": student_totals["active_students"],
        "active_students_in_measured_groups": measured_students,
        "seat_balance": declared - measured_students,
        "attribution": "current_group_scope",
    }


def _risk_summary(
    context: ExecutiveSummaryContext,
    *,
    student_scope: Q,
    lower: datetime,
    upper: datetime,
    include_finance: bool,
) -> dict[str, Any]:
    students = StudentProfile.objects.filter(
        student_scope,
        status=StudentProfile.Status.ACTIVE,
    ).order_by()
    attendance_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="lesson__cohort__branch_id",
        department_field="lesson__cohort__department_id",
    )
    attendance_records = AttendanceRecord.objects.filter(
        attendance_scope,
        lesson__starts_at__gte=lower,
        lesson__starts_at__lt=upper,
    )
    assessment_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="exam__cohort__branch_id",
        department_field="exam__cohort__department_id",
    )
    exam_results = ExamResult.objects.filter(
        assessment_scope,
        exam__is_published=True,
        exam__exam_date__gte=context.window.date_from,
        exam__exam_date__lte=context.window.date_to,
    )
    overdue_invoices = None
    if include_finance:
        invoice_scope = _scope_predicate(
            context.scope.boundaries,
            branch_field="branch_at_issue_id",
            department_field="department_at_issue_id",
        )
        overdue_invoices = Invoice.objects.filter(
            invoice_scope,
            attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
            status=Invoice.Status.OVERDUE,
        )
    sample = students.count()
    attendance, grades, overdue = _risk_signal_data(
        students=students,
        attendance_records=attendance_records,
        exam_results=exam_results,
        overdue_invoices=overdue_invoices,
    )
    low_attendance = {student_id for student_id, row in attendance.items() if _is_low_attendance(row)}
    low_grades = {student_id for student_id, average in grades.items() if average < LOW_GRADE_PCT_THRESHOLD}
    at_risk = low_attendance | low_grades | overdue
    high_risk = low_attendance & (low_grades | overdue)
    medium_risk = (low_attendance - low_grades - overdue) | ((low_grades & overdue) - low_attendance)
    low_risk = (low_grades ^ overdue) - low_attendance
    totals: dict[str, Any] = {
        "student_sample_size": sample,
        "at_risk_students": len(at_risk),
        "high_risk_students": len(high_risk),
        "medium_risk_students": len(medium_risk),
        "low_risk_students": len(low_risk),
        "low_attendance_students": len(low_attendance),
        "low_grade_students": len(low_grades),
        "overdue_payment_students": len(overdue),
    }
    totals["at_risk_rate_fraction"] = round(totals["at_risk_students"] / sample, 4) if sample else None
    totals["included_signals"] = [
        "low_attendance",
        "low_grades",
        *(("overdue_payment",) if include_finance else ()),
    ]
    totals["finance_signal_included"] = include_finance
    return totals


def _teacher_summary(
    context: ExecutiveSummaryContext,
    *,
    lower: datetime,
    upper: datetime,
) -> dict[str, Any]:
    from apps.academics.models import Exam
    from apps.teachers.models import TeacherProfile

    profile_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="branch_id",
        department_field="department_id",
    )
    cohort_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="cohort__branch_id",
        department_field="cohort__department_id",
    )
    attendance_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="lesson__cohort__branch_id",
        department_field="lesson__cohort__department_id",
    )
    teacher_totals = TeacherProfile.objects.filter(profile_scope).aggregate(
        teacher_count=Count("id"),
        active_teacher_count=Count("id", filter=Q(is_active=True)),
    )
    lessons = Lesson.objects.filter(
        cohort_scope,
        starts_at__gte=lower,
        starts_at__lt=upper,
    ).order_by()
    lesson_totals = lessons.aggregate(
        completed_lessons=Count("id", filter=Q(status=Lesson.Status.COMPLETED)),
        teachers_delivering=Count(
            "teacher_id",
            filter=Q(status=Lesson.Status.COMPLETED),
            distinct=True,
        ),
        groups_delivered=Count(
            "cohort_id",
            filter=Q(status=Lesson.Status.COMPLETED),
            distinct=True,
        ),
    )
    marks = AttendanceRecord.objects.filter(
        attendance_scope,
        lesson__starts_at__gte=lower,
        lesson__starts_at__lt=upper,
    ).order_by()
    mark_totals = marks.aggregate(
        attendance_numerator=Count(
            "id",
            filter=Q(status__in=(AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE)),
        ),
        attendance_denominator=Count(
            "id",
            filter=~Q(status=AttendanceRecord.Status.EXCUSED),
        ),
        students_reached=Count("student_id", distinct=True),
        lessons_with_attendance=Count("lesson_id", distinct=True),
    )
    exam_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="exam__cohort__branch_id",
        department_field="exam__cohort__department_id",
    )
    assessment_totals = ExamResult.objects.filter(
        exam_scope,
        exam__is_published=True,
        exam__exam_date__gte=context.window.date_from,
        exam__exam_date__lte=context.window.date_to,
    ).aggregate(
        published_exams_with_results=Count("exam_id", distinct=True),
        graded_results=Count("id"),
        assessed_students=Count("student_id", distinct=True),
    )
    # Published exams without results are readiness evidence too; report them
    # separately instead of pretending they contributed assessment samples.
    published_exams = Exam.objects.filter(
        cohort_scope,
        is_published=True,
        exam_date__gte=context.window.date_from,
        exam_date__lte=context.window.date_to,
    ).count()
    denominator = mark_totals["attendance_denominator"]
    return {
        **teacher_totals,
        **lesson_totals,
        **mark_totals,
        **assessment_totals,
        "published_exams": published_exams,
        "attendance_rate_fraction": (
            round(mark_totals["attendance_numerator"] / denominator, 4) if denominator else None
        ),
    }


def _task_attention(context: ExecutiveSummaryContext) -> dict[str, int]:
    from apps.tasks.models import Task

    if context.scope.organization_wide:
        scope = Q()
    else:
        scope = _scope_predicate(
            context.scope.boundaries,
            branch_field="branch_id",
            department_field="department_id",
        )
    tasks = Task.objects.filter(
        scope,
        assignee_principal_kind=context.principal_kind,
        assignee_principal_id=context.principal_id,
        assignee_attribution_status="captured",
        status__in=(Task.Status.OPEN, Task.Status.IN_PROGRESS, Task.Status.BLOCKED),
    ).order_by()
    totals = tasks.aggregate(
        open_assigned_to_me=Count("id"),
        blocked_assigned_to_me=Count("id", filter=Q(status=Task.Status.BLOCKED)),
        overdue_assigned_to_me=Count(
            "id",
            filter=Q(due_at__lt=context.generated_at),
        ),
    )
    return totals


def _branch_only_scope(context: ExecutiveSummaryContext) -> bool:
    return all(boundary.department_id is None for boundary in context.scope.boundaries)


def _pending_approval_count(
    context: ExecutiveSummaryContext,
    *,
    include_compensation: bool,
) -> int:
    from apps.approvals.models import ApprovalRequest
    from apps.approvals.services import KIND_SALARY_PREP

    requests = ApprovalRequest.objects.filter(status=ApprovalRequest.Status.PENDING)
    if not context.scope.organization_wide:
        requests = requests.filter(
            branch_id__in=[boundary.branch_id for boundary in context.scope.boundaries]
        )
    if not include_compensation:
        requests = requests.exclude(kind=KIND_SALARY_PREP)
    return requests.count()


def _unread_notification_count(context: ExecutiveSummaryContext) -> int:
    from apps.notifications.models import (
        DELIVERABLE_ATTRIBUTION_STATUSES,
        Notification,
    )

    return Notification.objects.filter(
        user_id=context.user_id,
        recipient_principal_kind=context.principal_kind,
        recipient_principal_id=context.principal_id,
        attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
        read_at__isnull=True,
    ).count()


def _upcoming_meeting_count(
    context: ExecutiveSummaryContext,
    *,
    upper: datetime,
) -> int:
    from apps.meetings.models import MeetingAttendee, StaffMeeting

    invitations = MeetingAttendee.objects.filter(
        principal_kind=context.principal_kind,
        principal_id=context.principal_id,
        meeting__status=StaffMeeting.Status.SCHEDULED,
        meeting__starts_at__gte=context.generated_at,
        meeting__starts_at__lt=upper,
    )
    if not context.scope.organization_wide:
        invitations = invitations.filter(
            meeting__branch_id__in=[boundary.branch_id for boundary in context.scope.boundaries]
        )
    return invitations.values("meeting_id").distinct().count()


def _mark_scope_unrepresentable(
    coverage: dict[str, dict[str, Any]],
    warnings: list[dict[str, Any]],
    *,
    section: str,
    message: str,
) -> None:
    coverage[section]["status"] = "omitted"
    coverage[section]["reason"] = "scope_not_representable"
    warnings.append(
        {
            "code": "scope_not_representable",
            "message": message,
            "affected_sections": [section],
        }
    )


def _finance_summary(
    context: ExecutiveSummaryContext,
    *,
    lower: datetime,
    upper: datetime,
) -> dict[str, Any]:
    invoice_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="branch_at_issue_id",
        department_field="department_at_issue_id",
    )
    invoices = Invoice.objects.filter(
        invoice_scope,
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        issue_date__gte=context.window.date_from,
        issue_date__lte=context.window.date_to,
    ).order_by()
    invoice_totals = invoices.aggregate(
        billed=Sum(
            "total_uzs",
            filter=~Q(status__in=(Invoice.Status.DRAFT, Invoice.Status.VOID)),
        ),
        open_total=Sum("total_uzs", filter=Q(status__in=_OPEN_INVOICE_STATUSES)),
        overdue_invoices=Count("id", filter=Q(status=Invoice.Status.OVERDUE)),
    )

    payment_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="branch_at_payment_id",
        department_field="department_at_payment_id",
    )
    attributed_payments = Payment.objects.filter(
        payment_scope,
        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        status__in=(Payment.Status.COMPLETED, Payment.Status.REFUNDED),
    ).order_by()
    collected = (
        attributed_payments.filter(paid_at__gte=lower, paid_at__lt=upper).aggregate(total=Sum("amount_uzs"))[
            "total"
        ]
        or _ZERO
    )

    allocation_invoice_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="invoice__branch_at_issue_id",
        department_field="invoice__department_at_issue_id",
    )
    allocations = PaymentAllocation.objects.filter(
        allocation_invoice_scope,
        invoice__attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
        payment_id__in=Subquery(attributed_payments.values("pk")),
    )
    allocation_totals = allocations.aggregate(
        allocated_to_open_window_invoices=Sum(
            "amount_uzs",
            filter=Q(
                invoice__status__in=_OPEN_INVOICE_STATUSES,
                invoice__issue_date__gte=context.window.date_from,
                invoice__issue_date__lte=context.window.date_to,
            ),
        ),
    )
    open_total = invoice_totals["open_total"] or _ZERO
    allocated = allocation_totals["allocated_to_open_window_invoices"] or _ZERO
    outstanding = max(open_total - allocated, _ZERO)

    refund_scope = _scope_predicate(
        context.scope.boundaries,
        branch_field="invoice__branch_at_issue_id",
        department_field="invoice__department_at_issue_id",
    )
    refunded = (
        Refund.objects.filter(
            refund_scope,
            invoice__attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
            payment_id__in=Subquery(attributed_payments.values("pk")),
            state=Refund.State.COMPLETED,
            provider_confirmed_at__gte=lower,
            provider_confirmed_at__lt=upper,
        ).aggregate(total=Sum("amount_uzs"))["total"]
        or _ZERO
    )

    result = {
        "billed": _money(invoice_totals["billed"], context.currency),
        "collected": _money(collected, context.currency),
        "outstanding_for_invoices_issued_in_window": _money(outstanding, context.currency),
        "overdue_invoice_count": invoice_totals["overdue_invoices"],
        "refunded": _money(refunded, context.currency),
    }

    if all(boundary.department_id is None for boundary in context.scope.boundaries):
        expense_branch_ids = [boundary.branch_id for boundary in context.scope.boundaries]
        expenses = Expense.objects.filter(branch_id__in=expense_branch_ids).order_by()
        expense_totals = expenses.aggregate(
            approved=Sum(
                "amount_uzs",
                filter=Q(
                    status__in=(Expense.Status.APPROVED, Expense.Status.PAID),
                    approved_at__gte=lower,
                    approved_at__lt=upper,
                ),
            ),
            paid=Sum(
                "amount_uzs",
                filter=Q(
                    status=Expense.Status.PAID,
                    paid_at__gte=lower,
                    paid_at__lt=upper,
                ),
            ),
        )
        result["approved_expense"] = _money(expense_totals["approved"], context.currency)
        result["paid_expense"] = _money(expense_totals["paid"], context.currency)
    return result


def _money(value: Decimal | None, currency: str) -> dict[str, int | str]:
    amount = value or _ZERO
    return {
        "amount_minor": int((amount * 100).to_integral_exact()),
        "currency": currency,
    }
