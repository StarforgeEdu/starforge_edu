"""Schedule write services (TASKS section 9, TD-12)."""

from __future__ import annotations

import datetime as dt
import uuid

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, YEARLY, rrulestr
from django.apps import apps as django_apps
from django.core import signing
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.schedule.models import Lesson, RecurrenceRule, Term
from apps.schedule.selectors import check_conflicts, check_occurrence_conflicts
from apps.schedule.signals import (
    lesson_cancelled,
    lesson_reminder_due,
    lesson_rescheduled,
    lessons_bulk_rescheduled,
)
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
# A recurrence rule represents one class slot per calendar day at most. Sub-daily
# frequencies are nonsensical with the model's fixed start/end time and can expand
# to millions of datetimes before the old date de-duplication ran.
ALLOWED_RULE_FREQUENCIES = frozenset((DAILY, WEEKLY, MONTHLY, YEARLY))
MAX_RULE_OCCURRENCES = 1000
MAX_RRULE_EXPANSION_CANDIDATES = 2000
MAX_RRULE_LENGTH = 2048
DISALLOWED_FIXED_TIME_COMPONENTS = frozenset(("BYHOUR", "BYMINUTE", "BYSECOND"))


# ---------------------------------------------------------------------------
# Recurrence materialization
# ---------------------------------------------------------------------------


def _aware(date: dt.date, time: dt.time):
    return timezone.make_aware(dt.datetime.combine(date, time), timezone.get_current_timezone())


def validate_rrule(rrule_str: str, *, start_date: dt.date, start_time: dt.time) -> None:
    if not isinstance(rrule_str, str) or not rrule_str.strip() or len(rrule_str) > MAX_RRULE_LENGTH:
        raise ValidationException(
            _("Invalid recurrence rule."),
            code="invalid_rrule",
            fields={"rrule": [f"A non-empty rule of at most {MAX_RRULE_LENGTH} characters is required."]},
        )
    normalized = rrule_str.strip()
    # Only a single RRULE is accepted. DTSTART/RDATE/EXRULE blocks would turn this
    # fixed-time model into an attacker-controlled rruleset with very different cost.
    if "\r" in normalized or "\n" in normalized:
        raise ValidationException(
            _("Invalid recurrence rule."),
            code="invalid_rrule",
            fields={"rrule": ["Only a single RRULE is allowed."]},
        )
    body = normalized[6:] if normalized.upper().startswith("RRULE:") else normalized
    component_names = {part.partition("=")[0].strip().upper() for part in body.split(";")}
    disallowed = sorted(component_names & DISALLOWED_FIXED_TIME_COMPONENTS)
    if disallowed:
        raise ValidationException(
            _("Recurrence time must use the rule's fixed start_time."),
            code="rrule_time_component_not_allowed",
            fields={"rrule": [f"These components are not allowed: {', '.join(disallowed)}."]},
        )
    try:
        parsed = rrulestr(normalized, dtstart=dt.datetime.combine(start_date, start_time))
    except (ValueError, TypeError) as exc:
        raise ValidationException(_("Invalid recurrence rule."), code="invalid_rrule") from exc
    if getattr(parsed, "_freq", None) not in ALLOWED_RULE_FREQUENCIES:
        raise ValidationException(
            _("Recurrence frequency must be daily or less frequent."),
            code="rrule_frequency_too_frequent",
            fields={"rrule": ["Use DAILY, WEEKLY, MONTHLY, or YEARLY."]},
        )


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
    # Revalidate here as well as on create/update so legacy or directly-created rows
    # cannot bypass the resource bounds by calling materialize_rule directly.
    validate_rrule(rule.rrule, start_date=rule.start_date, start_time=rule.start_time)
    rset = rrulestr(rule.rrule, dtstart=naive_start)
    holidays = _holiday_dates(rule.cohort.branch_id)
    seen: set[dt.date] = set()
    occurrences: list[tuple[dt.datetime, dt.datetime]] = []
    # xafter is lazy; unlike between(), it never allocates an attacker-controlled
    # multi-million occurrence list before we can enforce the cap.
    for candidate_count, occ in enumerate(rset.xafter(naive_start, inc=True), start=1):
        if occ > naive_until:
            break
        # Holidays and same-day de-duplication must not permit an enormous raw
        # expansion to burn CPU while yielding only a small lesson list.
        if candidate_count > MAX_RRULE_EXPANSION_CANDIDATES:
            raise ValidationException(
                _("Recurrence creates too many candidate dates."),
                code="rrule_too_many_occurrences",
                fields={"rrule": [f"At most {MAX_RULE_OCCURRENCES} occurrences are allowed."]},
            )
        date = occ.date()
        if date in holidays or date in seen:
            continue
        if len(occurrences) >= MAX_RULE_OCCURRENCES:
            raise ValidationException(
                _("Recurrence creates too many lessons."),
                code="rrule_too_many_occurrences",
                fields={"rrule": [f"At most {MAX_RULE_OCCURRENCES} occurrences are allowed."]},
            )
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

    occurrences = [
        (starts_at, ends_at)
        for starts_at, ends_at in _rule_occurrences(rule)
        if starts_at > now and starts_at not in kept_starts
    ]
    conflicts = check_occurrence_conflicts(
        occurrences,
        cohort_id=rule.cohort_id,
        teacher_id=rule.teacher_id,
        room_id=rule.room_id,
        exclude_lesson_ids=[lf.id for lf in kept],
    )
    if conflicts:
        raise ConflictException(_("Schedule conflict."), code="schedule_conflict", fields=conflicts)

    to_create = []
    for starts_at, ends_at in occurrences:
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
    try:
        # A concurrent lesson insert can race the friendly pre-check; the exclusion
        # constraints decide the winner, and the savepoint keeps this transaction usable.
        with transaction.atomic():
            Lesson.objects.bulk_create(to_create)
    except IntegrityError:
        raise ConflictException(_("Schedule conflict."), code="schedule_conflict") from None
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
    # A moved occurrence must be eligible for one-time absence reconciliation at its
    # new time. Existing attendance remains authoritative and will not be overwritten.
    lesson.auto_absence_processed_at = None
    lesson.save(
        update_fields=[
            "starts_at",
            "ends_at",
            "detached_from_rule",
            "auto_absence_processed_at",
            "updated_at",
        ]
    )
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


def _emit_bulk_rescheduled(
    *,
    cohort_id: int,
    moves: tuple[dict[str, object], ...],
    actor_id: int | None,
    schema: str,
):
    """Build the single post-commit event for a rule-wide move.

    The tuple contains primitive JSON-safe snapshots only.  Each operation also
    carries a stable random ``move_id``: wall clocks can repeat under clock
    correction/frozen tests, while a retry must retain the same idempotency key.
    """

    def _send() -> None:
        lessons_bulk_rescheduled.send(
            sender=Lesson,
            cohort_id=cohort_id,
            moves=moves,
            actor_id=actor_id,
            schema_name=schema,
        )

    return _send


@transaction.atomic
def bulk_reschedule(rule: RecurrenceRule, *, shift_minutes: int, actor=None) -> int:
    """Shift every FUTURE scheduled lesson of the rule by `shift_minutes`,
    all-or-nothing: any induced conflict rolls the whole batch back.  One aggregate
    post-commit signal carries every shifted occurrence to bounded asynchronous
    notification fan-out; the request never dispatches lessons x recipients inline."""
    delta = dt.timedelta(minutes=shift_minutes)
    now = timezone.now()
    lessons = list(
        rule.lessons.select_for_update()
        .filter(status=Lesson.Status.SCHEDULED, starts_at__gt=now)
        .order_by("pk")
    )
    moved_ids = [lf.id for lf in lessons]
    old_starts = {lf.id: lf.starts_at for lf in lessons}
    shifted_occurrences = [(lesson.starts_at + delta, lesson.ends_at + delta) for lesson in lessons]
    conflicts = check_occurrence_conflicts(
        shifted_occurrences,
        cohort_id=rule.cohort_id,
        teacher_id=rule.teacher_id,
        room_id=rule.room_id,
        exclude_lesson_ids=moved_ids,
    )
    if conflicts:
        raise ConflictException(_("Schedule conflict."), code="schedule_conflict", fields=conflicts)
    if not lessons:
        return 0

    # PostgreSQL checks each non-deferrable exclusion constraint while the write
    # happens. Saving a weekly batch row-by-row can therefore make the first row's
    # new slot collide with the second row's *old* slot, even though the final batch
    # is conflict-free. Temporarily move every locked row outside the constraint's
    # ``status='scheduled'`` predicate, update all times, then restore them together.
    # The outer transaction makes the temporary state invisible and guarantees that
    # any failure restores the original schedule.
    moved_at = timezone.now()
    moving = Lesson.objects.filter(pk__in=moved_ids)
    try:
        with transaction.atomic():
            moving.update(status=Lesson.Status.CANCELLED)
            for lesson, (new_start, new_end) in zip(lessons, shifted_occurrences, strict=True):
                lesson.starts_at = new_start
                lesson.ends_at = new_end
                lesson.updated_at = moved_at
                # Keep the same invariant as move_occurrence: changing a lesson's
                # time makes it eligible for absence reconciliation at the new time.
                lesson.auto_absence_processed_at = None
            Lesson.objects.bulk_update(
                lessons,
                ["starts_at", "ends_at", "auto_absence_processed_at", "updated_at"],
                batch_size=500,
            )
            moving.update(status=Lesson.Status.SCHEDULED)
    except IntegrityError as exc:
        # A concurrent insert can still win after the pre-check; surface the same
        # stable API conflict instead of leaking a database exception.
        raise ConflictException(_("Schedule conflict."), code="schedule_conflict") from exc

    for lesson in lessons:
        lesson.status = Lesson.Status.SCHEDULED
    schema = current_schema()
    actor_id = getattr(actor, "pk", None)
    moves = tuple(
        {
            "lesson_id": lesson.pk,
            "old_start": old_starts[lesson.id].isoformat(),
            "moved_at": lesson.updated_at.isoformat(),
            # Stable across fan-out retries, distinct for every actual move even
            # when the application clock repeats.
            "move_id": uuid.uuid4().hex,
        }
        for lesson in lessons
    )
    transaction.on_commit(
        _emit_bulk_rescheduled(
            cohort_id=rule.cohort_id,
            moves=moves,
            actor_id=actor_id,
            schema=schema,
        )
    )
    return len(moves)


# ---------------------------------------------------------------------------
# iCal feed (signed token, tenant-bound)
# ---------------------------------------------------------------------------


def ical_token_for(user) -> str:
    return signing.dumps(
        {"user_id": user.pk, "schema": current_schema(), "tv": user.token_version}, salt=ICAL_SALT
    )


def lessons_for_token(token: str):
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
    cutoff = timezone.now() - dt.timedelta(days=ICAL_WINDOW_DAYS)
    return scoped_lessons(user=user).filter(starts_at__gte=cutoff).order_by("starts_at")[:ICAL_MAX_LESSONS]


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
    ).select_related("cohort")
    schema = current_schema()
    count = 0
    for lesson in due:
        from apps.cohorts.progression import lesson_cycle_signal

        cycle_signal = lesson_cycle_signal(lesson)
        lesson.reminder_sent_at = now
        lesson.save(update_fields=["reminder_sent_at"])
        lesson_reminder_due.send(
            sender=Lesson,
            lesson_id=lesson.pk,
            schema_name=schema,
            **cycle_signal,
        )
        count += 1
    return count


def archive_ended_term_lessons() -> int:
    """Archive scheduled lessons whose term has ended (idempotent filter-update)."""
    today = timezone.localdate()
    return Lesson.objects.filter(status=Lesson.Status.SCHEDULED, term__end_date__lt=today).update(
        status=Lesson.Status.ARCHIVED
    )
