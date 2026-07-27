"""Schedule write services (TASKS section 9, TD-12)."""

from __future__ import annotations

import datetime as dt

from dateutil.rrule import rrulestr
from django.apps import apps as django_apps
from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.schedule.models import Lesson, RecurrenceRule, Term
from apps.schedule.selectors import check_conflicts
from apps.schedule.signals import lesson_cancelled, lesson_reminder_due, lesson_rescheduled
from core.exceptions import ConflictException, ValidationException
from core.utils import current_schema

ICAL_SALT = "schedule.ical"
# A leaked feed URL grants the user's schedule until the token expires; bound to
# token_version so password-change / logout also revokes outstanding feeds.
ICAL_TOKEN_MAX_AGE = dt.timedelta(days=30)
# Bound the feed so a director's (whole-tenant) calendar can't materialize years of
# accumulated lesson occurrences into one multi-MB response on every poll: a rolling
# recent-past window (calendars only need near-past for context) plus a hard row cap.
ICAL_WINDOW_DAYS = 90
ICAL_MAX_LESSONS = 2000


# ---------------------------------------------------------------------------
# Recurrence materialization
# ---------------------------------------------------------------------------


def _aware(date: dt.date, time: dt.time):
    return timezone.make_aware(dt.datetime.combine(date, time), timezone.get_current_timezone())


def validate_rrule(rrule_str: str, *, start_date: dt.date, start_time: dt.time) -> None:
    try:
        rrulestr(rrule_str, dtstart=dt.datetime.combine(start_date, start_time))
    except (ValueError, TypeError) as exc:
        raise ValidationException(_("Invalid recurrence rule."), code="invalid_rrule") from exc


def _holiday_dates(branch_id: int) -> set[dt.date]:
    BranchHoliday = django_apps.get_model("org", "BranchHoliday")
    return set(
        BranchHoliday.objects.filter(branch_id=branch_id, is_working_day_override=False).values_list(
            "date", flat=True
        )
    )


def _rule_occurrences(rule: RecurrenceRule) -> list[tuple]:
    """Expand the rule to (starts_at, ends_at) pairs in the [start_date, end_date]
    window, skipping holiday dates for the cohort's branch. Naive rrule expansion
    then localized — Asia/Tashkent has no DST so wall-clock times are stable."""
    naive_start = dt.datetime.combine(rule.start_date, rule.start_time)
    naive_until = dt.datetime.combine(rule.end_date, dt.time(23, 59, 59))
    rset = rrulestr(rule.rrule, dtstart=naive_start)
    holidays = _holiday_dates(rule.cohort.branch_id)
    seen: set[dt.date] = set()
    occurrences = []
    for occ in rset.between(naive_start, naive_until, inc=True):
        date = occ.date()
        if date in holidays or date in seen:
            continue
        seen.add(date)
        occurrences.append((_aware(date, rule.start_time), _aware(date, rule.end_time)))
    return occurrences


def _has_attendance(lesson_ids) -> set[int]:
    """Lesson ids that already have attendance (never deleted on re-materialize).
    No-op until Lane B's AttendanceRecord exists."""
    try:
        AttendanceRecord = django_apps.get_model("attendance", "AttendanceRecord")
    except LookupError:
        return set()
    return set(
        AttendanceRecord.objects.filter(lesson_id__in=list(lesson_ids))
        .values_list("lesson_id", flat=True)
        .distinct()
    )


@transaction.atomic
def materialize_rule(rule: RecurrenceRule) -> list[Lesson]:
    """Expand `rule` into Lesson rows. Idempotent: re-running replaces only
    FUTURE, non-detached, attendance-free lessons of this rule; past, detached,
    or attended lessons are preserved. Conflicts (room/teacher/cohort overlap
    with OTHER lessons) abort the whole operation with 409."""
    now = timezone.now()

    existing = list(rule.lessons.all())
    attended = _has_attendance([lf.id for lf in existing])
    kept = [lf for lf in existing if lf.detached_from_rule or lf.id in attended or lf.starts_at <= now]
    kept_starts = {lf.starts_at for lf in kept}
    regenerable_ids = [lf.id for lf in existing if lf not in kept]
    Lesson.objects.filter(id__in=regenerable_ids).delete()

    # A deactivated rule purges its regenerable (future, non-detached,
    # attendance-free) lessons and stops generating new ones — otherwise
    # is_active would be a misleading no-op control. Past/detached/attended
    # lessons are preserved exactly like the re-materialize path above.
    if not rule.is_active:
        return list(rule.lessons.order_by("starts_at"))

    to_create = []
    for starts_at, ends_at in _rule_occurrences(rule):
        if starts_at <= now or starts_at in kept_starts:
            continue
        conflicts = check_conflicts(
            starts_at=starts_at,
            ends_at=ends_at,
            cohort_id=rule.cohort_id,
            teacher_id=rule.teacher_id,
            room_id=rule.room_id,
            exclude_lesson_ids=[lf.id for lf in kept],
        )
        if conflicts:
            raise ConflictException(_("Schedule conflict."), code="schedule_conflict", fields=conflicts)
        to_create.append(
            Lesson(
                rule=rule,
                term=rule.term,
                cohort=rule.cohort,
                teacher=rule.teacher,
                room=rule.room,
                lesson_type=rule.lesson_type,
                title=rule.title,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        )
    Lesson.objects.bulk_create(to_create)
    return list(rule.lessons.order_by("starts_at"))


# ---------------------------------------------------------------------------
# Rule create / update
# ---------------------------------------------------------------------------


def _clamp_to_term(rule: RecurrenceRule) -> None:
    if rule.start_date < rule.term.start_date:
        rule.start_date = rule.term.start_date
    if rule.end_date > rule.term.end_date:
        rule.end_date = rule.term.end_date


@transaction.atomic
def create_rule(*, created_by=None, **data) -> RecurrenceRule:
    rule = RecurrenceRule(created_by=created_by, **data)
    validate_rrule(rule.rrule, start_date=rule.start_date, start_time=rule.start_time)
    _clamp_to_term(rule)
    rule.full_clean(exclude=["created_by"])
    rule.save()
    materialize_rule(rule)
    return rule


@transaction.atomic
def update_rule(rule: RecurrenceRule, **data) -> RecurrenceRule:
    for key, value in data.items():
        setattr(rule, key, value)
    validate_rrule(rule.rrule, start_date=rule.start_date, start_time=rule.start_time)
    _clamp_to_term(rule)
    rule.full_clean(exclude=["created_by"])
    rule.save()
    materialize_rule(rule)
    return rule


# ---------------------------------------------------------------------------
# One-off occurrence operations
# ---------------------------------------------------------------------------


@transaction.atomic
def cancel_occurrence(lesson: Lesson, *, reason: str = "", actor=None) -> Lesson:
    # Idempotent: a re-cancel (client retry / double-click) must not re-save or
    # re-emit lesson_cancelled, else D3-C fires duplicate cancellation notices.
    if lesson.status == Lesson.Status.CANCELLED:
        return lesson
    lesson.status = Lesson.Status.CANCELLED
    lesson.cancel_reason = reason
    lesson.save(update_fields=["status", "cancel_reason", "updated_at"])
    schema = current_schema()
    transaction.on_commit(
        lambda: lesson_cancelled.send(
            sender=Lesson, lesson_id=lesson.pk, actor_id=getattr(actor, "pk", None), schema_name=schema
        )
    )
    return lesson


@transaction.atomic
def move_occurrence(lesson: Lesson, *, starts_at, ends_at, actor=None) -> Lesson:
    # Only a scheduled lesson can be moved: a cancelled/archived lesson neither
    # conflicts nor blocks others (constraints are status='scheduled'-scoped), so
    # moving it would be a silent no-op state change that still detaches + emits.
    if lesson.status != Lesson.Status.SCHEDULED:
        raise ConflictException(_("Only a scheduled lesson can be moved."), code="lesson_not_scheduled")
    if ends_at <= starts_at:
        raise ValidationException(_("ends_at must be after starts_at."), code="invalid_times")
    conflicts = check_conflicts(
        starts_at=starts_at,
        ends_at=ends_at,
        cohort_id=lesson.cohort_id,
        teacher_id=lesson.teacher_id,
        room_id=lesson.room_id,
        exclude_lesson_ids=[lesson.id],
    )
    if conflicts:
        raise ConflictException(_("Schedule conflict."), code="schedule_conflict", fields=conflicts)
    old_start = lesson.starts_at
    lesson.starts_at = starts_at
    lesson.ends_at = ends_at
    lesson.detached_from_rule = True
    lesson.save(update_fields=["starts_at", "ends_at", "detached_from_rule", "updated_at"])
    schema = current_schema()
    # moved_at = the lesson's post-save updated_at: a monotonic, unique-per-move,
    # stored value. It's the notification dedupe discriminator so EVERY move notifies —
    # old_start alone re-collides whenever a lesson returns to a previously-occupied slot
    # (move A->B->A->B: the 3rd move's old_start=A equals the 1st's), silently dropping
    # the notification. updated_at is bumped on every save, so it never repeats.
    moved_at = lesson.updated_at.isoformat()
    transaction.on_commit(
        lambda: lesson_rescheduled.send(
            sender=Lesson,
            lesson_id=lesson.pk,
            old_start=old_start.isoformat(),
            moved_at=moved_at,
            actor_id=getattr(actor, "pk", None),
            schema_name=schema,
        )
    )
    return lesson


def _emit_rescheduled(*, lesson_id: int, old_start, moved_at: str, actor_id, schema: str):
    """Build an on_commit callback that emits lesson_rescheduled with kwargs
    matching move_occurrence — a factory binds the per-lesson values cleanly
    (avoids late-binding pitfalls of a loop lambda). ``moved_at`` (the moved lesson's
    updated_at) is the per-move dedupe discriminator (see move_occurrence)."""

    def _send():
        lesson_rescheduled.send(
            sender=Lesson,
            lesson_id=lesson_id,
            old_start=old_start.isoformat(),
            moved_at=moved_at,
            actor_id=actor_id,
            schema_name=schema,
        )

    return _send


@transaction.atomic
def bulk_reschedule(rule: RecurrenceRule, *, shift_minutes: int, actor=None) -> int:
    """Shift every FUTURE scheduled lesson of the rule by `shift_minutes`,
    all-or-nothing: any induced conflict rolls the whole batch back. Each shifted
    lesson emits lesson_rescheduled on commit (matching move_occurrence) so D3-C's
    on_lesson_rescheduled notifies students/parents — a bulk shift is the highest-
    impact reschedule and must not be silent."""
    delta = dt.timedelta(minutes=shift_minutes)
    now = timezone.now()
    lessons = list(rule.lessons.filter(status=Lesson.Status.SCHEDULED, starts_at__gt=now))
    moved_ids = [lf.id for lf in lessons]
    old_starts = {lf.id: lf.starts_at for lf in lessons}
    for lesson in lessons:
        new_start = lesson.starts_at + delta
        new_end = lesson.ends_at + delta
        conflicts = check_conflicts(
            starts_at=new_start,
            ends_at=new_end,
            cohort_id=lesson.cohort_id,
            teacher_id=lesson.teacher_id,
            room_id=lesson.room_id,
            exclude_lesson_ids=moved_ids,
        )
        if conflicts:
            raise ConflictException(_("Schedule conflict."), code="schedule_conflict", fields=conflicts)
        lesson.starts_at = new_start
        lesson.ends_at = new_end
    for lesson in lessons:
        lesson.save(update_fields=["starts_at", "ends_at", "updated_at"])
    schema = current_schema()
    actor_id = getattr(actor, "pk", None)
    for lesson in lessons:
        transaction.on_commit(
            _emit_rescheduled(
                lesson_id=lesson.pk,
                old_start=old_starts[lesson.id],
                moved_at=lesson.updated_at.isoformat(),  # per-move dedupe discriminator
                actor_id=actor_id,
                schema=schema,
            )
        )
    return len(lessons)


# ---------------------------------------------------------------------------
# iCal feed (signed token, tenant-bound)
# ---------------------------------------------------------------------------


def ical_token_for(user) -> str:
    return signing.dumps(
        {"user_id": user.pk, "schema": current_schema(), "tv": user.token_version}, salt=ICAL_SALT
    )


def lessons_for_token(token: str):
    from rest_framework_simplejwt.exceptions import TokenError

    from apps.schedule.selectors import scoped_lessons
    from apps.users.models import User
    from core.exceptions import AuthenticationException

    try:
        data = signing.loads(token, salt=ICAL_SALT, max_age=ICAL_TOKEN_MAX_AGE)
    except signing.BadSignature as exc:
        # SignatureExpired is a BadSignature subclass — expired tokens land here too.
        raise AuthenticationException(_("Invalid feed token."), code="authentication_failed") from exc
    if data.get("schema") != current_schema():
        raise AuthenticationException(_("This feed belongs to a different center."), code="tenant_mismatch")
    user = User.objects.filter(pk=data.get("user_id")).first()
    if user is None or not user.is_active:
        # A deactivated account's feed URL must stop leaking schedule data.
        raise AuthenticationException(_("Invalid feed token."), code="authentication_failed")
    # token_version mismatch ⇒ password-change / logout revoked this feed.
    if data.get("tv") != user.token_version:
        raise AuthenticationException(_("Invalid feed token."), code="authentication_failed")
    try:
        cutoff = timezone.now() - dt.timedelta(days=ICAL_WINDOW_DAYS)
        return scoped_lessons(user=user).filter(starts_at__gte=cutoff).order_by("starts_at")[
            :ICAL_MAX_LESSONS
        ]
    except TokenError:  # pragma: no cover - defensive
        return Lesson.objects.none()


def build_ical(lessons) -> bytes:
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//Starforge Edu//Schedule//EN")
    cal.add("version", "2.0")
    for lesson in lessons:
        event = Event()
        event.add("uid", f"lesson-{lesson.pk}@starforge")
        event.add("summary", lesson.title)
        event.add("dtstart", lesson.starts_at)
        event.add("dtend", lesson.ends_at)
        if lesson.status == Lesson.Status.CANCELLED:
            event.add("status", "CANCELLED")
        cal.add_component(event)
    return cal.to_ical()


def current_term() -> Term | None:
    return Term.objects.filter(is_current=True).first()


# ---------------------------------------------------------------------------
# Beat task bodies (emit-only; D3-C wires notification dispatch)
# ---------------------------------------------------------------------------


def emit_due_reminders() -> int:
    """Emit `lesson_reminder_due` for scheduled lessons starting in 25-35 min,
    once each. `reminder_sent_at` IS the idempotency key — a re-run skips them
    (DoD #9). Runs under the active tenant schema."""
    now = timezone.now()
    due = Lesson.objects.filter(
        status=Lesson.Status.SCHEDULED,
        reminder_sent_at__isnull=True,
        starts_at__gte=now + dt.timedelta(minutes=25),
        starts_at__lte=now + dt.timedelta(minutes=35),
    )
    schema = current_schema()
    count = 0
    for lesson in due:
        lesson.reminder_sent_at = now
        lesson.save(update_fields=["reminder_sent_at"])
        lesson_reminder_due.send(sender=Lesson, lesson_id=lesson.pk, schema_name=schema)
        count += 1
    return count


def archive_ended_term_lessons() -> int:
    """Archive scheduled lessons whose term has ended (idempotent filter-update)."""
    today = timezone.localdate()
    return Lesson.objects.filter(status=Lesson.Status.SCHEDULED, term__end_date__lt=today).update(
        status=Lesson.Status.ARCHIVED
    )
