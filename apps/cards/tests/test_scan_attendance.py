"""F12/F15 — a door card-scan feeds attendance: a VALID scan marks the student PRESENT on
the active-cohort lesson they're arriving for. It never overrides a teacher's mark and
never creates an absence (safe for the A-1 absence-deduction money path)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


def _setup(tenant, *, lesson_offset_min=0, with_lesson=True, active_card=True):
    """A student in an active cohort with an issued card; optionally a SCHEDULED lesson
    starting `lesson_offset_min` from now. Returns dict of the created rows (inside schema)."""
    from apps.cards.models import Card, CardType
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)  # active membership
        lesson = None
        if with_lesson:
            start = timezone.now() + timedelta(minutes=lesson_offset_min)
            lesson = Lesson.objects.create(
                term=TermFactory(),
                cohort=cohort,
                teacher=TeacherProfileFactory(branch=branch),
                title="Lesson",
                starts_at=start,
                ends_at=start + timedelta(hours=1),
            )
        card = Card.objects.create(
            student=student,
            card_type=CardType.objects.create(name="ID"),
            code=f"CARD-{student.id}",
            is_active=active_card,
        )
        return {"student": student, "cohort": cohort, "lesson": lesson, "code": card.code}


def _scan(tenant, code):
    from apps.cards.services import scan_card

    with schema_context(tenant.schema_name):
        return scan_card(code=code)


def _records(tenant, student, lesson):
    from apps.attendance.models import AttendanceRecord

    with schema_context(tenant.schema_name):
        return list(AttendanceRecord.objects.filter(student=student, lesson=lesson))


def test_scan_marks_present_on_the_current_lesson(tenant_a):
    s = _setup(tenant_a, lesson_offset_min=0)
    result = _scan(tenant_a, s["code"])
    assert result["valid"] is True
    assert result["attendance_lesson"] == s["lesson"].id

    records = _records(tenant_a, s["student"], s["lesson"])
    assert len(records) == 1
    assert records[0].status == "present"
    assert records[0].auto_marked is True
    assert records[0].note == "card_scan"


def test_scan_does_not_override_an_existing_mark(tenant_a):
    """A teacher's mark always wins — a later scan must not flip it."""
    from apps.attendance.models import AttendanceRecord

    s = _setup(tenant_a, lesson_offset_min=0)
    with schema_context(tenant_a.schema_name):
        AttendanceRecord.objects.create(
            student=s["student"], lesson=s["lesson"], status=AttendanceRecord.Status.ABSENT
        )
    result = _scan(tenant_a, s["code"])
    assert result["attendance_lesson"] is None  # not re-marked
    records = _records(tenant_a, s["student"], s["lesson"])
    assert len(records) == 1
    assert records[0].status == "absent"  # the existing mark is untouched


def test_scan_outside_the_window_marks_nothing(tenant_a):
    """A lesson far from the scan time (here +3h) is not the one being arrived for."""
    s = _setup(tenant_a, lesson_offset_min=180)
    result = _scan(tenant_a, s["code"])
    assert result["valid"] is True
    assert result["attendance_lesson"] is None
    assert _records(tenant_a, s["student"], s["lesson"]) == []


def test_scan_with_no_lesson_is_a_plain_checkin(tenant_a):
    s = _setup(tenant_a, with_lesson=False)
    result = _scan(tenant_a, s["code"])
    assert result["valid"] is True
    assert result["attendance_lesson"] is None  # nothing to mark; just the door log


def test_scan_marks_the_lesson_being_arrived_for_not_an_earlier_one(tenant_a):
    """Regression (self-review): with a just-ended lesson AND an imminent one both inside the
    ±30-min window, the scan marks the one the student is ARRIVING for (nearest start), not
    the earliest-started (already-ended) one — so back-to-back classes aren't mis-attributed."""
    from apps.cards.models import Card, CardType
    from apps.cohorts.tests.factories import CohortFactory, CohortMembershipFactory
    from apps.org.tests.factories import BranchFactory
    from apps.schedule.models import Lesson
    from apps.schedule.tests.factories import TermFactory
    from apps.students.tests.factories import StudentProfileFactory
    from apps.teachers.tests.factories import TeacherProfileFactory

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        cohort = CohortFactory(branch=branch)
        student = StudentProfileFactory(branch=branch)
        CohortMembershipFactory(cohort=cohort, student=student)
        term = TermFactory()
        teacher = TeacherProfileFactory(branch=branch)
        now = timezone.now()
        earlier = Lesson.objects.create(  # ended ~20 min ago, still inside the window
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="A",
            starts_at=now - timedelta(minutes=50),
            ends_at=now - timedelta(minutes=20),
        )
        arriving = Lesson.objects.create(  # starts in ~10 min — the one being arrived for
            term=term,
            cohort=cohort,
            teacher=teacher,
            title="B",
            starts_at=now + timedelta(minutes=10),
            ends_at=now + timedelta(minutes=40),
        )
        card = Card.objects.create(
            student=student,
            card_type=CardType.objects.create(name="ID"),
            code=f"CARD-{student.id}",
            is_active=True,
        )

    result = _scan(tenant_a, card.code)
    assert result["attendance_lesson"] == arriving.id  # B, not the ended A
    assert _records(tenant_a, student, earlier) == []  # the earlier lesson is untouched


def test_invalid_card_scan_marks_no_attendance(tenant_a):
    """A revoked card is logged but never checks the student into a lesson."""
    from apps.attendance.models import AttendanceRecord

    s = _setup(tenant_a, lesson_offset_min=0, active_card=False)
    result = _scan(tenant_a, s["code"])
    assert result["valid"] is False
    assert result["attendance_lesson"] is None
    with schema_context(tenant_a.schema_name):
        assert not AttendanceRecord.objects.filter(student=s["student"]).exists()
