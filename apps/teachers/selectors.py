"""Teacher read selectors."""

from __future__ import annotations

from django.db.models import Q, QuerySet
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

    audience = Q(audience_user_ids__contains=[user.pk])
    for role in roles:
        audience |= Q(audience_roles__contains=[str(role)])

    already_answered = FormResponse.objects.filter(form=OuterRef("pk"), respondent=user)
    forms = (
        Form.objects.filter(status=Form.Status.PUBLISHED)
        .filter(Q(opens_at__isnull=True) | Q(opens_at__lte=now))
        .filter(Q(closes_at__isnull=True) | Q(closes_at__gte=now))
        .filter(Q(branch__isnull=True) | Q(branch_id=teacher.branch_id))
        .filter(audience)
        .annotate(_answered=Exists(already_answered))
        .filter(_answered=False)
        .order_by("closes_at", "created_at")[:10]
    )
    return [
        {"id": f.id, "title": f.title, "closes_at": f.closes_at}
        for f in forms
    ]


def teacher_dashboard(*, teacher: TeacherProfile, user, roles) -> dict:
    """A single read over the teacher's groups, schedule (with lesson types), exams,
    expected graduations, outstanding rule acknowledgments, and forms to fill (F3-2)."""
    from apps.academics.models import Exam
    from apps.cohorts.models import Cohort, CohortMembership
    from apps.compliance import selectors as compliance_selectors
    from apps.schedule.models import Lesson

    now = timezone.now()
    today = now.date()

    cohorts = Cohort.objects.filter(
        Q(primary_teacher=teacher) | Q(co_teachers__teacher=teacher)
    ).distinct()
    cohort_ids = list(cohorts.values_list("id", flat=True))

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
            "cohort": lesson.cohort.name,
            "starts_at": lesson.starts_at,
            "ends_at": lesson.ends_at,
            "lesson_type": lesson.lesson_type.name if lesson.lesson_type else None,
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
        for cohort in cohorts.filter(end_date__gte=today).order_by("end_date")[:10]
    ]

    from apps.meetings.services import next_meeting_for

    next_meeting = next_meeting_for(user, now=now)
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
    }
