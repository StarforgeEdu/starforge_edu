"""D2-F-6 conflict-detection property tests.

Table-driven overlap cases x dimension (room/teacher/cohort) x path (service
`check_conflicts` AND the raw-ORM exclusion constraint) — proving the GiST
exclusion catches what a service path might miss, and that touching edges
(end == start) are NOT a conflict (half-open ranges)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.cohorts.tests.factories import CohortFactory
from apps.org.tests.factories import BranchFactory, RoomFactory
from apps.schedule import services
from apps.schedule.models import Lesson
from apps.schedule.tests.factories import TermFactory
from apps.teachers.tests.factories import TeacherProfileFactory

pytestmark = pytest.mark.django_db


def _at(hh, mm=0, *, day=6):
    return timezone.make_aware(datetime(2026, 7, day, hh, mm))


# (label, candidate_start, candidate_end, overlaps) — base lesson is 14:00-15:00.
CASES = [
    ("disjoint", _at(16), _at(17), False),
    ("touching_before", _at(13), _at(14), False),  # end == base.start
    ("touching_after", _at(15), _at(16), False),  # start == base.end
    ("contained", _at(14, 15), _at(14, 45), True),
    ("spanning", _at(13, 30), _at(15, 30), True),
    ("identical", _at(14), _at(15), True),
    ("overlap_start", _at(13, 30), _at(14, 30), True),
    ("overlap_end", _at(14, 30), _at(15, 30), True),
]
DIMENSIONS = ["teacher", "cohort", "room"]


def _ctx():
    branch = BranchFactory()
    base_teacher: Any = TeacherProfileFactory(branch=branch)
    base_cohort: Any = CohortFactory(branch=branch)
    base_room: Any = RoomFactory(branch=branch)
    term: Any = TermFactory()
    base = Lesson.objects.create(
        term=term,
        cohort=base_cohort,
        teacher=base_teacher,
        room=base_room,
        title="Base",
        starts_at=_at(14),
        ends_at=_at(15),
    )
    return branch, term, base


def _candidate_entities(branch, base, dimension):
    """Share ONLY `dimension`'s entity with the base; differ on the others so a
    single exclusion constraint is exercised in isolation."""
    teacher = base.teacher if dimension == "teacher" else TeacherProfileFactory(branch=branch)
    cohort = base.cohort if dimension == "cohort" else CohortFactory(branch=branch)
    room = base.room if dimension == "room" else RoomFactory(branch=branch)
    return teacher, cohort, room


@pytest.mark.parametrize(("label", "start", "end", "overlaps"), CASES)
@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_conflict_service_path(tenant_a, dimension, label, start, end, overlaps):
    with schema_context(tenant_a.schema_name):
        branch, _term, base = _ctx()
        teacher, cohort, room = _candidate_entities(branch, base, dimension)
        conflicts = services.check_conflicts(
            starts_at=start, ends_at=end, cohort_id=cohort.id, teacher_id=teacher.id, room_id=room.id
        )
        if overlaps:
            assert conflicts.get(dimension) == [base.id], f"{label}/{dimension}"
            assert set(conflicts) == {dimension}  # only the shared dimension conflicts
        else:
            assert conflicts == {}, f"{label}/{dimension} should be clear"


@pytest.mark.parametrize(("label", "start", "end", "overlaps"), CASES)
@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_conflict_orm_exclusion(tenant_a, dimension, label, start, end, overlaps):
    with schema_context(tenant_a.schema_name):
        branch, term, base = _ctx()
        teacher, cohort, room = _candidate_entities(branch, base, dimension)

        def _create():
            return Lesson.objects.create(
                term=term,
                cohort=cohort,
                teacher=teacher,
                room=room,
                title="Candidate",
                starts_at=start,
                ends_at=end,
            )

        if overlaps:
            with pytest.raises(IntegrityError), transaction.atomic():
                _create()
        else:
            assert _create().pk is not None  # touching/disjoint is allowed


def test_cross_midnight_overlap(tenant_a):
    """A lesson spanning midnight conflicts with one starting after midnight."""
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        teacher: Any = TeacherProfileFactory(branch=branch)
        cohort: Any = CohortFactory(branch=branch)
        term: Any = TermFactory()
        night_start = _at(23, day=6)
        night_end = _at(1, day=7)
        Lesson.objects.create(
            term=term, cohort=cohort, teacher=teacher, title="Night", starts_at=night_start, ends_at=night_end
        )
        # 00:30-02:00 next day overlaps the 23:00-01:00 block on the teacher.
        cand_start = _at(0, 30, day=7)
        cand_end = _at(2, day=7)
        other_cohort: Any = CohortFactory(branch=branch)
        conflicts = services.check_conflicts(
            starts_at=cand_start, ends_at=cand_end, cohort_id=other_cohort.id, teacher_id=teacher.id
        )
        assert "teacher" in conflicts
        early_cohort: Any = CohortFactory(branch=branch)
        with pytest.raises(IntegrityError), transaction.atomic():
            Lesson.objects.create(
                term=term,
                cohort=early_cohort,
                teacher=teacher,
                title="Early",
                starts_at=cand_start,
                ends_at=cand_end,
            )
        # 02:00-03:00 is clear.
        assert (
            services.check_conflicts(
                starts_at=_at(2, day=7), ends_at=_at(3, day=7), cohort_id=cohort.id, teacher_id=teacher.id
            )
            == {}
        )
