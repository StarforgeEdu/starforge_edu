"""Schedule lane tests (D2-A): materialization, conflicts (service + DB-level
exclusion), one-off ops, iCal, scoping, query budget."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.cohorts.tests.factories import CohortFactory
from apps.org.models import BranchHoliday
from apps.org.tests.factories import BranchFactory, RoomFactory
from apps.schedule import selectors, services
from apps.schedule.models import Lesson
from apps.schedule.tests.factories import TermFactory
from apps.teachers.tests.factories import TeacherProfileFactory
from core.exceptions import ConflictException, ValidationException

pytestmark = pytest.mark.django_db


def _aware(y, m, d, hh, mm=0):
    return timezone.make_aware(datetime(y, m, d, hh, mm))


def _at(day: date, hh: int, mm: int = 0):
    """Aware datetime on `day` — the relative-date counterpart of `_aware`."""
    return timezone.make_aware(datetime(day.year, day.month, day.day, hh, mm))


# `materialize_rule` only ever creates occurrences STRICTLY IN THE FUTURE
# (`if starts_at <= now: continue`). Anchoring the rule window to a hardcoded
# calendar date therefore silently stops materializing lessons once that date
# passes, turning these tests into false failures (and, worse, hiding real
# regressions). Anchor every window to the next Monday instead.
_WINDOW_DAYS = 25  # Mon + Wed for 4 weeks = 8 occurrences, all inside the window
_TERM_PAD = timedelta(days=180)


def _anchor_monday() -> date:
    """The first Monday STRICTLY in the future, so a 14:00 lesson on it is future."""
    today = timezone.localdate()
    return today + timedelta(days=((0 - today.weekday()) % 7 or 7))


def _setup(*, term_end=None):
    branch = BranchFactory()
    anchor = _anchor_monday()
    return {
        "branch": branch,
        "anchor": anchor,
        "term": TermFactory(
            start_date=anchor - _TERM_PAD,
            end_date=term_end if term_end is not None else anchor + _TERM_PAD,
        ),
        "cohort": CohortFactory(branch=branch),
        "teacher": TeacherProfileFactory(branch=branch),
        "room": RoomFactory(branch=branch),
    }


_UNSET = object()


def _make_rule(ctx, *, rrule="FREQ=WEEKLY;BYDAY=MO,WE", start=_UNSET, end=_UNSET):
    start_date = ctx["anchor"] if start is _UNSET else start
    end_date = start_date + timedelta(days=_WINDOW_DAYS) if end is _UNSET else end
    return services.create_rule(
        term=ctx["term"],
        cohort=ctx["cohort"],
        teacher=ctx["teacher"],
        room=ctx["room"],
        title="Algebra",
        rrule=rrule,
        start_date=start_date,
        end_date=end_date,
        start_time=time(14, 0),
        end_time=time(15, 30),
    )


def test_materialize_counts_and_holiday_skip(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        BranchHoliday.objects.create(branch=ctx["branch"], date=ctx["anchor"], name="Holiday")
        rule = _make_rule(ctx)
        # anchor +0,+2,+7,+9,+14,+16,+21,+23 = 8 Mon/Wed slots, minus the anchor holiday = 7.
        assert rule.lessons.count() == 7
        assert not rule.lessons.filter(starts_at__date=ctx["anchor"]).exists()


def test_materialize_idempotent(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        first = set(rule.lessons.values_list("starts_at", flat=True))
        services.materialize_rule(rule)
        second = set(rule.lessons.values_list("starts_at", flat=True))
        assert first == second
        assert rule.lessons.count() == 8


def test_detached_lesson_survives_rematerialize(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        lesson = rule.lessons.order_by("starts_at").first()
        away = ctx["anchor"] + timedelta(days=28)  # past the rule window, still inside the term
        services.move_occurrence(lesson, starts_at=_at(away, 9), ends_at=_at(away, 10))
        services.materialize_rule(rule)
        lesson.refresh_from_db()
        assert lesson.detached_from_rule is True
        assert rule.lessons.filter(pk=lesson.pk).exists()


def test_exclusion_constraint_blocks_raw_orm_overlap(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        start = _at(ctx["anchor"], 14)
        Lesson.objects.create(
            term=ctx["term"],
            cohort=ctx["cohort"],
            teacher=ctx["teacher"],
            title="A",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Lesson.objects.create(
                term=ctx["term"],
                cohort=ctx["cohort"],
                teacher=ctx["teacher"],
                title="B",
                starts_at=start + timedelta(minutes=30),
                ends_at=start + timedelta(minutes=90),
            )


@pytest.mark.parametrize("dimension", ["teacher", "cohort", "room"])
def test_conflict_overlap_raises_409(tenant_a, dimension):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        anchor = ctx["anchor"]
        _make_rule(ctx, rrule="FREQ=WEEKLY;BYDAY=MO", start=anchor, end=anchor)
        # A second rule overlapping on the chosen dimension, same Monday 14:00.
        other = dict(ctx)
        if dimension == "teacher":
            other["cohort"] = CohortFactory(branch=ctx["branch"])
            other["room"] = RoomFactory(branch=ctx["branch"])
        elif dimension == "cohort":
            other["teacher"] = TeacherProfileFactory(branch=ctx["branch"])
            other["room"] = RoomFactory(branch=ctx["branch"])
        else:  # room
            other["teacher"] = TeacherProfileFactory(branch=ctx["branch"])
            other["cohort"] = CohortFactory(branch=ctx["branch"])
        with pytest.raises(ConflictException) as exc:
            _make_rule(other, rrule="FREQ=WEEKLY;BYDAY=MO", start=anchor, end=anchor)
        assert exc.value.code == "schedule_conflict"
        # D2-A-3: conflicting ids sit directly under error.fields[dimension].
        assert dimension in (exc.value.fields or {})


def test_adjacent_lessons_allowed(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        start = _at(ctx["anchor"], 14)
        Lesson.objects.create(
            term=ctx["term"],
            cohort=ctx["cohort"],
            teacher=ctx["teacher"],
            title="A",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
        )
        # 15:00-16:00 touches the edge — NOT a conflict.
        conflicts = services.check_conflicts(
            starts_at=start + timedelta(hours=1),
            ends_at=start + timedelta(hours=2),
            cohort_id=ctx["cohort"].id,
            teacher_id=ctx["teacher"].id,
            room_id=ctx["room"].id,
        )
        assert conflicts == {}


def test_bulk_reschedule_atomic_rollback_on_conflict(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        # Park a fixed lesson where a +1-day shift of the first lesson would land,
        # so the shift induces a conflict and must roll the whole batch back.
        first = rule.lessons.order_by("starts_at").first()
        blocker_start = first.starts_at + timedelta(days=1)
        blocker_cohort: Any = CohortFactory(branch=ctx["branch"])
        Lesson.objects.create(
            term=ctx["term"],
            cohort=blocker_cohort,
            teacher=ctx["teacher"],
            title="Blocker",
            starts_at=blocker_start,
            ends_at=blocker_start + timedelta(hours=1),
        )
        before = list(rule.lessons.order_by("starts_at").values_list("starts_at", flat=True))
        with pytest.raises(ConflictException):
            services.bulk_reschedule(rule, shift_minutes=24 * 60)
        after = list(rule.lessons.order_by("starts_at").values_list("starts_at", flat=True))
        assert before == after  # nothing moved


def test_bulk_reschedule_emits_one_rescheduled_per_moved_lesson(tenant_a, django_capture_on_commit_callbacks):
    """Regression: bulk_reschedule must emit lesson_rescheduled (on_commit) once
    per shifted lesson, mirroring move_occurrence, so D3-C notifies students."""
    received: list[int] = []

    def _recv(sender, lesson_id, **kw):
        received.append(lesson_id)

    services.lesson_rescheduled.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            ctx = _setup()
            rule = _make_rule(ctx)
            expected_ids = sorted(rule.lessons.values_list("id", flat=True))
            assert len(expected_ids) == 8
            with django_capture_on_commit_callbacks(execute=True):
                moved = services.bulk_reschedule(rule, shift_minutes=30)
            assert moved == 8
        assert sorted(received) == expected_ids
    finally:
        services.lesson_rescheduled.disconnect(_recv)


def test_bulk_reschedule_passes_actor_and_old_start(tenant_a, user_in, django_capture_on_commit_callbacks):
    """The emitted kwargs must match move_occurrence: lesson_id, old_start
    (isoformat of the pre-shift start), actor_id, schema_name."""
    actor = user_in(tenant_a, roles=["director"])
    captured: list[dict] = []

    def _recv(sender, **kw):
        captured.append(kw)

    services.lesson_rescheduled.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            ctx = _setup()
            rule = _make_rule(ctx)
            old_starts = {lf.id: lf.starts_at for lf in rule.lessons.all()}
            with django_capture_on_commit_callbacks(execute=True):
                services.bulk_reschedule(rule, shift_minutes=30, actor=actor)
        assert captured  # at least one emit
        for kw in captured:
            assert kw["actor_id"] == actor.pk
            assert kw["schema_name"] == tenant_a.schema_name
            assert kw["old_start"] == old_starts[kw["lesson_id"]].isoformat()
    finally:
        services.lesson_rescheduled.disconnect(_recv)


def test_deactivating_rule_clears_future_lessons_and_stops_regeneration(tenant_a):
    """Regression: setting is_active=False must purge the rule's regenerable
    (future, non-detached, attendance-free) lessons and stop re-materializing —
    otherwise is_active is a misleading no-op control."""
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        assert rule.lessons.count() == 8
        # Deactivate via the same service the API update path uses.
        services.update_rule(rule, is_active=False)
        assert rule.lessons.count() == 0
        # Re-materializing a deactivated rule does not regenerate occurrences.
        services.materialize_rule(rule)
        assert rule.lessons.count() == 0


def test_deactivating_rule_preserves_detached_lessons(tenant_a):
    """A detached (manually moved) lesson is preserved even when the rule is
    deactivated, mirroring the re-materialize keep-set."""
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        moved = rule.lessons.order_by("starts_at").first()
        away = ctx["anchor"] + timedelta(days=28)
        services.move_occurrence(moved, starts_at=_at(away, 9), ends_at=_at(away, 10))
        services.update_rule(rule, is_active=False)
        moved.refresh_from_db()
        assert rule.lessons.filter(pk=moved.pk).exists()
        # Only the detached lesson survives; the regenerable future ones are gone.
        assert rule.lessons.count() == 1


def test_ical_feed_valid_and_cross_tenant_rejected(tenant_a, tenant_b, user_in):
    from icalendar import Calendar

    from core.exceptions import AuthenticationException

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        _make_rule(ctx)
        token = services.ical_token_for(user)
        feed = services.build_ical(services.lessons_for_token(token))
        assert Calendar.from_ical(feed)["prodid"]
    # Same token on tenant_b → tenant_mismatch.
    with schema_context(tenant_b.schema_name):
        with pytest.raises(AuthenticationException) as exc:
            services.lessons_for_token(token)
        assert exc.value.code == "tenant_mismatch"


def test_invalid_rrule_rejected(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        with pytest.raises(ValidationException) as exc:
            _make_rule(ctx, rrule="THIS-IS-NOT-AN-RRULE")
        assert exc.value.code == "invalid_rrule"


# --- role scoping (D2-A-6 teacher, multi-cohort student/parent) ---


def _make_one_lesson(*, ctx, title, start=None):
    start = start or (timezone.now() + timedelta(days=1))
    return Lesson.objects.create(
        term=ctx["term"],
        cohort=ctx["cohort"],
        teacher=ctx["teacher"],
        room=ctx["room"],
        title=title,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
    )


def test_teacher_ical_feed_excludes_other_teachers_lesson(tenant_a, user_in):
    """D2-A-6: a teacher's personal feed shows only their own taught lessons,
    never another teacher's — the existing feed test only used a director."""
    teacher_a_user = user_in(tenant_a, roles=["teacher"])
    teacher_b_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        prof_a = TeacherProfileFactory(user=teacher_a_user, branch=branch)
        prof_b = TeacherProfileFactory(user=teacher_b_user, branch=branch)
        term = TermFactory()
        ctx_a = {"term": term, "cohort": CohortFactory(branch=branch), "teacher": prof_a, "room": None}
        ctx_b = {"term": term, "cohort": CohortFactory(branch=branch), "teacher": prof_b, "room": None}
        lesson_a = _make_one_lesson(ctx=ctx_a, title="A-taught")
        lesson_b = _make_one_lesson(ctx=ctx_b, title="B-taught")

        feed_ids = set(
            services.lessons_for_token(services.ical_token_for(teacher_a_user)).values_list("id", flat=True)
        )
        assert lesson_a.id in feed_ids
        assert lesson_b.id not in feed_ids


def test_ical_feed_excludes_lessons_older_than_the_window(tenant_a, user_in):
    """Scale (audit): the iCal feed is bounded to a recent-past window so a whole-tenant
    calendar can't materialize years of accumulated lessons on every poll. A lesson older
    than the window is excluded; a recent/future one is kept."""
    teacher_user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        prof = TeacherProfileFactory(user=teacher_user, branch=branch)
        term = TermFactory()
        ctx = {"term": term, "cohort": CohortFactory(branch=branch), "teacher": prof, "room": None}
        recent = _make_one_lesson(ctx=ctx, title="recent", start=timezone.now() + timedelta(days=1))
        old = _make_one_lesson(ctx=ctx, title="old", start=timezone.now() - timedelta(days=200))
        feed_ids = set(
            services.lessons_for_token(services.ical_token_for(teacher_user)).values_list("id", flat=True)
        )
        assert recent.id in feed_ids
        assert old.id not in feed_ids  # older than ICAL_WINDOW_DAYS


def test_student_sees_lessons_from_both_active_cohorts(tenant_a, user_in):
    """A multi-cohort student (active membership in A and B via the enroll
    service) must see lessons from BOTH cohorts — the schedule lane now scopes
    via active CohortMembership like attendance/assignments/content."""
    from apps.cohorts.services import enroll_student_in_cohort
    from apps.students.tests.factories import StudentProfileFactory

    student_user = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        term = TermFactory()
        student: Any = StudentProfileFactory(user=student_user, branch=branch)
        cohort_a: Any = CohortFactory(branch=branch)
        cohort_b: Any = CohortFactory(branch=branch)
        enroll_student_in_cohort(cohort=cohort_a, student=student)
        enroll_student_in_cohort(cohort=cohort_b, student=student)
        # Distinct teachers per cohort so the two concurrent lessons don't trip the
        # teacher-overlap exclusion constraint.
        ctx_a = {
            "term": term,
            "cohort": cohort_a,
            "teacher": TeacherProfileFactory(branch=branch),
            "room": None,
        }
        ctx_b = {
            "term": term,
            "cohort": cohort_b,
            "teacher": TeacherProfileFactory(branch=branch),
            "room": None,
        }
        lesson_a = _make_one_lesson(ctx=ctx_a, title="cohort-A")
        lesson_b = _make_one_lesson(ctx=ctx_b, title="cohort-B")

        visible = set(selectors.scoped_lessons(user=student_user).values_list("id", flat=True))
        assert {lesson_a.id, lesson_b.id} <= visible


def test_parent_sees_childs_active_cohort_lessons(tenant_a, user_in):
    """Parent variant of the membership-join scoping: a parent sees the lessons
    of their child's active cohorts."""
    from apps.cohorts.services import enroll_student_in_cohort
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    parent_user = user_in(tenant_a, roles=["parent"])
    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        term = TermFactory()
        parent = ParentProfileFactory(user=parent_user)
        student: Any = StudentProfileFactory(branch=branch)
        GuardianFactory(parent=parent, student=student)
        cohort: Any = CohortFactory(branch=branch)
        other_cohort: Any = CohortFactory(branch=branch)
        enroll_student_in_cohort(cohort=cohort, student=student)
        # Distinct teachers so the two concurrent lessons don't trip the
        # teacher-overlap exclusion constraint.
        ctx = {"term": term, "cohort": cohort, "teacher": TeacherProfileFactory(branch=branch), "room": None}
        ctx_other = {
            "term": term,
            "cohort": other_cohort,
            "teacher": TeacherProfileFactory(branch=branch),
            "room": None,
        }
        mine = _make_one_lesson(ctx=ctx, title="child-cohort")
        not_mine = _make_one_lesson(ctx=ctx_other, title="unrelated")

        visible = set(selectors.scoped_lessons(user=parent_user).values_list("id", flat=True))
        assert mine.id in visible
        assert not_mine.id not in visible


def test_student_rule_api_is_scoped_to_active_cohorts(tenant_a, user_in, as_user):
    from apps.cohorts.services import enroll_student_in_cohort
    from apps.students.tests.factories import StudentProfileFactory

    student_user = user_in(tenant_a, roles=["student"])
    with schema_context(tenant_a.schema_name):
        mine_ctx = _setup()
        other_ctx = _setup()
        student = StudentProfileFactory(user=student_user, branch=mine_ctx["branch"])
        enroll_student_in_cohort(cohort=mine_ctx["cohort"], student=student)
        mine = _make_rule(mine_ctx)
        other = _make_rule(other_ctx)

    client = as_user(tenant_a, student_user)
    listing = client.get("/api/v1/schedule/rules/")
    assert listing.status_code == 200
    assert {row["id"] for row in listing.json()["data"]} == {mine.id}
    assert client.get(f"/api/v1/schedule/rules/{mine.id}/").status_code == 200
    assert client.get(f"/api/v1/schedule/rules/{other.id}/").status_code == 404


def test_parent_rule_api_is_scoped_to_child_cohorts(tenant_a, user_in, as_user):
    from apps.cohorts.services import enroll_student_in_cohort
    from apps.parents.tests.factories import GuardianFactory, ParentProfileFactory
    from apps.students.tests.factories import StudentProfileFactory

    parent_user = user_in(tenant_a, roles=["parent"])
    with schema_context(tenant_a.schema_name):
        mine_ctx = _setup()
        other_ctx = _setup()
        parent = ParentProfileFactory(user=parent_user)
        student = StudentProfileFactory(branch=mine_ctx["branch"])
        GuardianFactory(parent=parent, student=student)
        enroll_student_in_cohort(cohort=mine_ctx["cohort"], student=student)
        mine = _make_rule(mine_ctx)
        other = _make_rule(other_ctx)

    client = as_user(tenant_a, parent_user)
    listing = client.get("/api/v1/schedule/rules/")
    assert listing.status_code == 200
    assert {row["id"] for row in listing.json()["data"]} == {mine.id}
    assert client.get(f"/api/v1/schedule/rules/{mine.id}/").status_code == 200
    assert client.get(f"/api/v1/schedule/rules/{other.id}/").status_code == 404


# --- one-off op guards ---


def test_move_rejects_non_scheduled_lesson(tenant_a):
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        lesson = rule.lessons.order_by("starts_at").first()
        services.cancel_occurrence(lesson, reason="snow day")
        lesson.refresh_from_db()
        away = ctx["anchor"] + timedelta(days=28)
        with pytest.raises(ConflictException) as exc:
            services.move_occurrence(lesson, starts_at=_at(away, 9), ends_at=_at(away, 10))
        assert exc.value.code == "lesson_not_scheduled"


def test_cancel_is_idempotent_no_re_emit(tenant_a, django_capture_on_commit_callbacks):
    received: list[int] = []

    def _recv(sender, lesson_id, **kw):
        received.append(lesson_id)

    services.lesson_cancelled.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            ctx = _setup()
            rule = _make_rule(ctx)
            lesson = rule.lessons.order_by("starts_at").first()
            with django_capture_on_commit_callbacks(execute=True):
                services.cancel_occurrence(lesson, reason="first")
            lesson.refresh_from_db()
            assert lesson.status == Lesson.Status.CANCELLED
            assert lesson.cancel_reason == "first"
            # Second cancel: short-circuits — no re-save (reason unchanged) and no
            # second on_commit emit.
            with django_capture_on_commit_callbacks(execute=True):
                services.cancel_occurrence(lesson, reason="second")
            lesson.refresh_from_db()
            assert lesson.cancel_reason == "first"
        assert received == [lesson.pk]
    finally:
        services.lesson_cancelled.disconnect(_recv)


# --- iCal token TTL + token_version ---


def test_ical_token_invalid_after_token_version_bump(tenant_a, user_in):
    from core.exceptions import AuthenticationException

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        _make_rule(ctx)
        token = services.ical_token_for(user)
        # Works before the bump.
        assert services.lessons_for_token(token) is not None
        # Logout-all / password-change bumps token_version → outstanding feed dies.
        user.token_version += 1
        user.save(update_fields=["token_version"])
        with pytest.raises(AuthenticationException) as exc:
            services.lessons_for_token(token)
        assert exc.value.code == "authentication_failed"


def test_ical_token_expired_rejected(tenant_a, user_in, monkeypatch):
    from core.exceptions import AuthenticationException

    user = user_in(tenant_a, roles=["director"])
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        _make_rule(ctx)
        token = services.ical_token_for(user)
        # Force an expired window so signing.loads(max_age=...) rejects the token.
        monkeypatch.setattr(services, "ICAL_TOKEN_MAX_AGE", timedelta(seconds=-1))
        with pytest.raises(AuthenticationException) as exc:
            services.lessons_for_token(token)
        assert exc.value.code == "authentication_failed"


# --- API surface ---


def test_rules_create_requires_write(tenant_a, as_role):
    from core.permissions import Role

    client, _ = as_role(Role.STUDENT)
    resp = client.post("/api/v1/schedule/rules/", {}, format="json")
    assert resp.status_code == 403


def test_rules_create_cross_branch_blocked(tenant_a, user_in, as_user):
    """R2-08: a branch-scoped schedule:write holder must not author a rule naming
    another branch's cohort/teacher/room (cross-branch injection + pk oracle). The
    rule has no branch column of its own, so the FK branches are scope-checked."""
    from core.permissions import Role

    with schema_context(tenant_a.schema_name):
        ctx_a = _setup()
        ctx_b = _setup()  # a DIFFERENT branch's cohort/teacher/room
    writer = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=ctx_a["branch"]))
    resp = writer.post(
        "/api/v1/schedule/rules/",
        {
            "term": ctx_b["term"].id,
            "cohort": ctx_b["cohort"].id,
            "teacher": ctx_b["teacher"].id,
            "room": ctx_b["room"].id,
            "title": "Injected",
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "start_date": ctx_b["anchor"].isoformat(),
            "end_date": (ctx_b["anchor"] + timedelta(days=_WINDOW_DAYS)).isoformat(),
            "start_time": "14:00",
            "end_time": "15:30",
        },
        format="json",
    )
    assert resp.status_code == 403, resp.content
    assert resp.json()["code"] == "out_of_scope"


def test_lesson_cancel_move_cross_branch_blocked(tenant_a, user_in, as_user):
    """SCHED-1: scoped_lessons returns EVERY lesson for STAFF_ROLES, but a branch-scoped
    HEAD_OF_DEPT/REGISTRAR must not cancel/move ANOTHER branch's lesson (cross-branch write +
    a cancellation/reschedule notification blast). Their own branch's lesson still works."""
    from core.permissions import Role

    with schema_context(tenant_a.schema_name):
        ctx_a = _setup()
        ctx_b = _setup()  # a DIFFERENT branch
        lesson_b_id = _make_rule(ctx_b).lessons.order_by("starts_at").first().id
        lesson_a_id = _make_rule(ctx_a).lessons.order_by("starts_at").first().id

    writer = as_user(tenant_a, user_in(tenant_a, roles=[Role.HEAD_OF_DEPT], branch=ctx_a["branch"]))

    # cross-branch cancel + move are refused (the branch check runs before the body is parsed)
    c = writer.post(f"/api/v1/schedule/lessons/{lesson_b_id}/cancel/", {"reason": "x"}, format="json")
    assert c.status_code == 403, c.content
    assert c.json()["code"] == "out_of_scope"
    m = writer.post(f"/api/v1/schedule/lessons/{lesson_b_id}/move/", {}, format="json")
    assert m.status_code == 403, m.content
    assert m.json()["code"] == "out_of_scope"

    # ...but the writer's OWN branch lesson still cancels (200)
    ok = writer.post(f"/api/v1/schedule/lessons/{lesson_a_id}/cancel/", {"reason": "snow"}, format="json")
    assert ok.status_code == 200, ok.content


def test_rules_create_reversed_times_is_400_not_500(tenant_a, as_role):
    """A rule whose start_time > end_time is regex-valid but violates the
    rule_times_ordered CheckConstraint; full_clean() raises Django's ValidationError.
    Without DRF's serializer layer that would be a hard 500 — the middleware safety
    net must render it as a clean 400 (owner rule: bad input never 500s)."""
    from core.permissions import Role

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
    resp = client.post(
        "/api/v1/schedule/rules/",
        {
            "term": ctx["term"].id,
            "cohort": ctx["cohort"].id,
            "teacher": ctx["teacher"].id,
            "title": "Bad hours",
            "rrule": "FREQ=WEEKLY;BYDAY=MO",
            "start_date": ctx["anchor"].isoformat(),
            "end_date": (ctx["anchor"] + timedelta(days=_WINDOW_DAYS)).isoformat(),
            "start_time": "15:00",
            "end_time": "14:00",
        },
        format="json",
    )
    assert resp.status_code == 400, resp.content


def test_bulk_reschedule_huge_shift_is_400_not_500(tenant_a, as_role):
    """An absurd shift_minutes would overflow datetime.timedelta -> raw 500. The view
    must bound it to a clean 400 (owner rule: bad input never 500s)."""
    from core.permissions import Role

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        rule = _make_rule(_setup())
    resp = client.post(
        f"/api/v1/schedule/rules/{rule.id}/bulk-reschedule/",
        {"shift_minutes": 10**18},
        format="json",
    )
    assert resp.status_code == 400, resp.content


def test_rule_put_omitting_is_active_preserves_it(tenant_a, as_role):
    """A PUT that omits is_active must NOT reactivate a deactivated rule (nor
    re-materialize its lessons) — parity with the old DRF SkipField behavior."""
    from core.permissions import Role

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        rule = _make_rule(ctx)
        rule.is_active = False
        rule.save(update_fields=["is_active"])
    resp = client.put(
        f"/api/v1/schedule/rules/{rule.id}/",
        {
            "term": ctx["term"].id,
            "cohort": ctx["cohort"].id,
            "teacher": ctx["teacher"].id,
            "room": ctx["room"].id,
            "title": "Algebra",
            "rrule": "FREQ=WEEKLY;BYDAY=MO,WE",
            "start_date": ctx["anchor"].isoformat(),
            "end_date": (ctx["anchor"] + timedelta(days=_WINDOW_DAYS)).isoformat(),
            "start_time": "14:00",
            "end_time": "15:30",
        },
        format="json",
    )
    assert resp.status_code == 200, resp.content
    rule.refresh_from_db()
    assert rule.is_active is False  # omitted is_active preserved, not forced True


def test_lesson_type_patch_bool_coercion_and_null_rejection(tenant_a, as_role):
    """PATCH coerces a string boolean (parity with create/bool_field) but rejects an
    explicit JSON null on the NOT-NULL is_active column."""
    from apps.schedule.models import LessonType
    from core.permissions import Role

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        lt = LessonType.objects.create(name="Speaking", slug="speaking", is_active=True)
    ok = client.patch(f"/api/v1/schedule/lesson-types/{lt.id}/", {"is_active": "false"}, format="json")
    assert ok.status_code == 200, ok.content
    lt.refresh_from_db()
    assert lt.is_active is False
    bad = client.patch(f"/api/v1/schedule/lesson-types/{lt.id}/", {"is_active": None}, format="json")
    assert bad.status_code == 400


def _make_due_lesson(ctx, *, title="Soon"):
    now = timezone.now()
    return Lesson.objects.create(
        term=ctx["term"],
        cohort=ctx["cohort"],
        teacher=ctx["teacher"],
        title=title,
        starts_at=now + timedelta(minutes=30),
        ends_at=now + timedelta(minutes=90),
    )


def test_reminder_task_idempotent_and_schema_scoped(tenant_a, tenant_b):
    from apps.schedule.services import emit_due_reminders
    from apps.schedule.signals import lesson_reminder_due

    received: list[int] = []

    def _recv(sender, lesson_id, **kw):
        received.append(lesson_id)

    # A due lesson in tenant_b that must stay untouched when the task runs under
    # tenant_a — emit_due_reminders is schema-scoped via the active connection.
    with schema_context(tenant_b.schema_name):
        b_lesson = _make_due_lesson(_setup(), title="OtherTenant")

    lesson_reminder_due.connect(_recv)
    try:
        with schema_context(tenant_a.schema_name):
            ctx = _setup()
            lesson = _make_due_lesson(ctx)
            assert emit_due_reminders() == 1
            lesson.refresh_from_db()
            assert lesson.reminder_sent_at is not None
            assert emit_due_reminders() == 0  # idempotent: reminder_sent_at is the key
        assert received == [lesson.pk]
    finally:
        lesson_reminder_due.disconnect(_recv)

    # tenant_b's lesson was never seen or stamped by the tenant_a run.
    assert b_lesson.pk not in received
    with schema_context(tenant_b.schema_name):
        b_lesson.refresh_from_db()
        assert b_lesson.reminder_sent_at is None


def test_archive_ended_term_lessons(tenant_a):
    from apps.schedule.services import archive_ended_term_lessons

    with schema_context(tenant_a.schema_name):
        branch = BranchFactory()
        objs: dict[str, Any] = {
            "term": TermFactory(
                academic_year="2020-2021",
                name="Ended",
                start_date=date(2020, 1, 1),
                end_date=date(2020, 12, 31),
            ),
            "cohort": CohortFactory(branch=branch),
            "teacher": TeacherProfileFactory(branch=branch),
        }
        lesson = Lesson.objects.create(
            term=objs["term"],
            cohort=objs["cohort"],
            teacher=objs["teacher"],
            title="Old",
            starts_at=_aware(2020, 3, 1, 14),
            ends_at=_aware(2020, 3, 1, 15),
        )
        assert archive_ended_term_lessons() == 1
        lesson.refresh_from_db()
        assert lesson.status == Lesson.Status.ARCHIVED


def test_lessons_list_includes_readable_names(tenant_a, as_role):
    """Additive denormalization: the lesson feed carries readable companions next to
    the bare FK ids (cohort_name / teacher_name / term_name / room_name /
    lesson_type_name) so a frontend needs no second call. The id fields are kept."""
    from core.permissions import Role

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        _make_rule(ctx)
        cohort_name = ctx["cohort"].name
        teacher_name = ctx["teacher"].user.get_full_name()
        room_name = ctx["room"].name
        term_name = ctx["term"].name
    body = client.get("/api/v1/schedule/lessons/").json()
    assert body["data"], body
    row = body["data"][0]
    # id fields preserved alongside the new readable companions
    assert row["cohort_name"] == cohort_name
    assert row["teacher_name"] == teacher_name
    assert row["term_name"] == term_name
    assert row["room_name"] == room_name
    # id fields are still present, not replaced
    assert row["cohort"] is not None
    assert row["teacher"] is not None
    # nullable lesson_type is unset on a materialized rule -> key present, value null
    assert row["lesson_type"] is None
    assert row["lesson_type_name"] is None


def test_lessons_list_query_budget(tenant_a, as_role, django_assert_max_num_queries):
    from core.permissions import Role

    client, _ = as_role(Role.DIRECTOR)
    with schema_context(tenant_a.schema_name):
        ctx = _setup()
        _make_rule(ctx)
        _make_rule(_setup())
    with django_assert_max_num_queries(9):  # +1: A-2 per-request permission-override load
        body = client.get("/api/v1/schedule/lessons/").json()
    assert set(body) == {"success", "data", "pagination"}
