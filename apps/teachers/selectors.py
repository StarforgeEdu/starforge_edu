"""Teacher read selectors."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.teachers.models import TeacherProfile


def list_teachers() -> QuerySet[TeacherProfile]:
    return TeacherProfile.objects.select_related("user", "branch", "department")


def teacher_profile_for(user) -> TeacherProfile | None:
    return TeacherProfile.objects.filter(user=user).first()


def _pending_forms_for(*, teacher: TeacherProfile, user, roles, now) -> list[dict]:
    """Published, currently-open forms that TARGET this teacher (by role or by user id) and
    that they have not yet answered — the "forms you must fill" dashboard warning (F3-2).
    An untargeted (open) form is not a personal to-do, so it never appears here."""
    from django.db.models import Exists, OuterRef, Q

    from apps.forms.models import Form, FormResponse

    audience = Q(audience_principals__contains=[{"kind": "teacher", "id": teacher.pk, "user_id": user.pk}])
    for role in roles:
        audience |= Q(audience_roles__contains=[str(role)])

    already_answered = FormResponse.objects.filter(
        form=OuterRef("pk"),
        respondent_principal_kind="teacher",
        respondent_principal_id=teacher.pk,
    )
    forms = (
        # Anonymous submissions intentionally carry no role principal, so the
        # service cannot truthfully mark one teacher's anonymous response as
        # completed. Keep such surveys available in /forms/ but do not present
        # them as individually trackable pending work.
        Form.objects.filter(status=Form.Status.PUBLISHED, is_anonymous=False)
        .filter(Q(opens_at__isnull=True) | Q(opens_at__lte=now))
        .filter(Q(closes_at__isnull=True) | Q(closes_at__gte=now))
        .filter(Q(branch__isnull=True) | Q(branch_id=teacher.branch_id))
        .filter(audience)
        .annotate(_answered=Exists(already_answered))
        .filter(_answered=False)
        .order_by("closes_at", "created_at")[:10]
    )
    return [{"id": f.id, "title": f.title, "closes_at": f.closes_at} for f in forms]


def _aware_start(day):
    return timezone.make_aware(datetime.combine(day, time.min), timezone.get_current_timezone())


def _dashboard_window(*, range_key: str, now):
    """Return a bounded reporting window accepted by the teacher dashboard."""

    today = timezone.localdate(now)
    if range_key == "30d":
        start_day = today - timedelta(days=29)
    elif range_key == "term":
        from apps.schedule.models import Term

        term = Term.objects.filter(is_current=True).first()
        start_day = term.start_date if term and term.start_date <= today else today - timedelta(days=83)
    else:
        range_key = "7d"
        start_day = today - timedelta(days=6)
    return range_key, _aware_start(start_day), now, start_day, today


def teacher_dashboard(*, teacher: TeacherProfile, user, roles, range_key: str = "7d") -> dict:
    """A single read over the teacher's groups, schedule (with lesson types), exams,
    expected graduations, outstanding rule acknowledgments, and forms to fill (F3-2)."""
    from apps.academics.models import Exam
    from apps.assignments.models import Submission
    from apps.attendance.models import AttendanceRecord
    from apps.cohorts.models import CohortMembership
    from apps.cohorts.progression import cohort_cycle_progress
    from apps.cohorts.selectors import taught_cohorts
    from apps.compliance import selectors as compliance_selectors
    from apps.schedule.models import Lesson

    now = timezone.now()
    today = now.date()
    range_key, window_start, window_end, start_day, local_today = _dashboard_window(
        range_key=range_key,
        now=now,
    )

    # Dashboard totals must reconcile with the group and student directories.
    # Cover/substitute lessons do not make a cohort part of the teacher's roster.
    cohorts = list(
        taught_cohorts(teacher=teacher, include_lesson_teacher=False)
        .select_related("branch", "department")
        .order_by("name")
    )
    cohort_ids = [cohort.id for cohort in cohorts]

    level_groups: dict[str, int] = {}
    for cohort in cohorts:
        key = cohort.level or "—"
        level_groups[key] = level_groups.get(key, 0) + 1

    students_count = (
        CohortMembership.objects.filter(cohort_id__in=cohort_ids, end_date__isnull=True)
        .values("student_id")
        .distinct()
        .count()
    )

    next_lessons = [
        {
            "id": lesson.id,
            "title": lesson.title,
            "cohort_id": lesson.cohort_id,
            "cohort": lesson.cohort.name,
            "starts_at": lesson.starts_at,
            "ends_at": lesson.ends_at,
            "lesson_type": lesson.lesson_type.name if lesson.lesson_type else None,
            "is_today": timezone.localtime(lesson.starts_at).date() == timezone.localdate(now),
        }
        for lesson in Lesson.objects.filter(
            teacher=teacher, starts_at__gte=now, status=Lesson.Status.SCHEDULED
        )
        .select_related("cohort", "lesson_type")
        .order_by("starts_at")[:5]
    ]

    upcoming_exams = [
        {"id": exam.id, "title": exam.title, "cohort": exam.cohort.name, "exam_date": exam.exam_date}
        for exam in Exam.objects.filter(cohort_id__in=cohort_ids, exam_date__gte=today)
        .select_related("cohort")
        .order_by("exam_date")[:5]
    ]

    graduations = [
        {"cohort": cohort.name, "end_date": cohort.end_date}
        for cohort in sorted(
            (item for item in cohorts if item.end_date >= today), key=lambda item: item.end_date
        )[:10]
    ]

    attendance_records = list(
        AttendanceRecord.objects.filter(
            lesson__cohort_id__in=cohort_ids,
            lesson__starts_at__gte=window_start,
            lesson__starts_at__lte=window_end,
        ).select_related("lesson")
    )
    attendance_by_day: dict = defaultdict(lambda: {"present": 0, "counted": 0})
    attendance_by_group: dict = defaultdict(lambda: {"present": 0, "counted": 0, "smart": 0, "warning": 0})
    for record in attendance_records:
        day = timezone.localtime(record.lesson.starts_at).date()
        day_bucket = attendance_by_day[day]
        group_bucket = attendance_by_group[record.lesson.cohort_id]
        if record.status in (AttendanceRecord.Status.PRESENT, AttendanceRecord.Status.LATE):
            day_bucket["present"] += 1
            group_bucket["present"] += 1
        if record.status in (
            AttendanceRecord.Status.PRESENT,
            AttendanceRecord.Status.LATE,
            AttendanceRecord.Status.ABSENT,
        ):
            day_bucket["counted"] += 1
            group_bucket["counted"] += 1
        if record.card_type == AttendanceRecord.CardType.SMART:
            group_bucket["smart"] += 1
        elif record.card_type == AttendanceRecord.CardType.WARNING:
            group_bucket["warning"] += 1

    attendance_trend = []
    for day in sorted(attendance_by_day):
        bucket = attendance_by_day[day]
        if bucket["counted"] == 0:
            continue
        attendance_trend.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%b %d"),
                "value": round(bucket["present"] / bucket["counted"] * 100, 1),
                "sample_size": bucket["counted"],
            }
        )

    week_start = local_today - timedelta(days=local_today.weekday())
    week_end = week_start + timedelta(days=7)
    teaching_load: defaultdict[date, int] = defaultdict(int)
    for lesson in Lesson.objects.filter(
        teacher=teacher,
        starts_at__gte=_aware_start(week_start),
        starts_at__lt=_aware_start(week_end),
    ).exclude(status__in=[Lesson.Status.CANCELLED, Lesson.Status.ARCHIVED]):
        teaching_load[timezone.localtime(lesson.starts_at).date()] += 1
    weekly_load = [
        {
            "date": (week_start + timedelta(days=offset)).isoformat(),
            "label": (week_start + timedelta(days=offset)).strftime("%a"),
            "value": teaching_load[week_start + timedelta(days=offset)],
        }
        for offset in range(7)
    ]

    historical_lessons = Lesson.objects.filter(
        cohort_id__in=cohort_ids,
        starts_at__gte=window_start,
        ends_at__lte=window_end,
    ).exclude(status__in=[Lesson.Status.CANCELLED, Lesson.Status.ARCHIVED])
    completed_lessons = historical_lessons.filter(status=Lesson.Status.COMPLETED)
    completed_lesson_count = historical_lessons.count()
    completed_with_register = completed_lessons.filter(attendance_records__isnull=False).distinct().count()
    completed_count = completed_lessons.count()

    submitted_work = Submission.objects.filter(
        assignment__cohort_id__in=cohort_ids,
        submitted_at__gte=window_start,
        submitted_at__lte=window_end,
    )
    submitted_count = submitted_work.count()
    reviewed_count = submitted_work.filter(status=Submission.Status.GRADED).count()
    score_breakdown = []
    if completed_count:
        score_breakdown.append(
            {
                "key": "attendance_coverage",
                "label": "Attendance recorded",
                "value": round(completed_with_register / completed_count * 100),
                "target": 100,
                "numerator": completed_with_register,
                "denominator": completed_count,
            }
        )
    if completed_lesson_count:
        score_breakdown.append(
            {
                "key": "lesson_completion",
                "label": "Lessons completed",
                "value": round(completed_count / completed_lesson_count * 100),
                "target": 100,
                "numerator": completed_count,
                "denominator": completed_lesson_count,
            }
        )
    if submitted_count:
        score_breakdown.append(
            {
                "key": "homework_review",
                "label": "Homework reviewed",
                "value": round(reviewed_count / submitted_count * 100),
                "target": 100,
                "numerator": reviewed_count,
                "denominator": submitted_count,
            }
        )

    membership_counts = {
        row["cohort_id"]: row["count"]
        for row in CohortMembership.objects.filter(
            cohort_id__in=cohort_ids,
            end_date__isnull=True,
        )
        .values("cohort_id")
        .annotate(count=Count("student_id", distinct=True))
    }
    group_health = []
    cycles_due = 0
    for cohort in cohorts:
        cycle = cohort_cycle_progress(cohort, at=now)
        if cycle["lessons_remaining_in_cycle"] <= 1:
            cycles_due += 1
        bucket = attendance_by_group[cohort.id]
        group_health.append(
            {
                "id": cohort.id,
                "name": cohort.name,
                "attendance": (
                    round(bucket["present"] / bucket["counted"] * 100, 1) if bucket["counted"] else None
                ),
                "attendance_sample_size": bucket["counted"],
                "up_cards": bucket["smart"],
                "down_cards": bucket["warning"],
                "students": membership_counts.get(cohort.id, 0),
                "level": cohort.level,
                "study_month": cohort.study_month,
                "lesson_cycle_length": cohort.lesson_cycle_length,
                "completed_in_cycle": cycle["completed_in_current_cycle"],
                "lessons_remaining_in_cycle": cycle["lessons_remaining_in_cycle"],
                "next_lesson": cycle["next_scheduled_lesson"],
            }
        )

    today_start = _aware_start(local_today)
    tomorrow_start = _aware_start(local_today + timedelta(days=1))
    lessons_today = Lesson.objects.filter(
        teacher=teacher,
        starts_at__gte=today_start,
        starts_at__lt=tomorrow_start,
    ).exclude(status__in=[Lesson.Status.CANCELLED, Lesson.Status.ARCHIVED])
    attendance_pending = (
        lessons_today.filter(ends_at__lte=now)
        .filter(Q(status=Lesson.Status.SCHEDULED) | Q(attendance_records__isnull=True))
        .distinct()
        .count()
    )
    grading_pending = Submission.objects.filter(
        assignment__cohort_id__in=cohort_ids,
        status=Submission.Status.SUBMITTED,
    ).count()

    from apps.meetings.services import next_meeting_for

    next_meeting = next_meeting_for(
        user,
        principal_kind="teacher",
        principal_id=teacher.pk,
        now=now,
    )
    return {
        "groups_count": len(cohort_ids),
        "students_count": students_count,
        "level_groups": level_groups,
        "next_lessons": next_lessons,
        "upcoming_exams": upcoming_exams,
        "expected_graduations": graduations,
        "next_meeting": (
            {
                "id": next_meeting.id,
                "title": next_meeting.title,
                "starts_at": next_meeting.starts_at,
                "location": next_meeting.location,
            }
            if next_meeting
            else None
        ),
        "pending_rule_acknowledgments": len(compliance_selectors.pending_rules(user, roles)),
        "pending_forms": _pending_forms_for(teacher=teacher, user=user, roles=roles, now=now),
        "reporting_range": {
            "key": range_key,
            "from": start_day.isoformat(),
            "to": local_today.isoformat(),
            "timezone": timezone.get_current_timezone_name(),
        },
        "attendance_trend": attendance_trend,
        "weekly_load": weekly_load,
        "score_breakdown": score_breakdown,
        "group_health": group_health,
        "action_summary": {
            "lessons_today": lessons_today.count(),
            "attendance_pending": attendance_pending,
            "grading_pending": grading_pending,
            "cycles_due": cycles_due,
        },
        "updated_at": now,
    }
