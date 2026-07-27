"""Notification receivers — the single bridge from domain signals to dispatch().

Connected in ``NotificationsConfig.ready()``. Domain apps emit emit-only signals
(inside ``transaction.on_commit``); these receivers resolve the recipient(s) and
call ``services.dispatch()`` once per recipient. Each receiver runs inside the
emitting tenant's schema (the signal carries ``schema_name`` for any cross-context
re-dispatch, but since signals fire synchronously after commit on the request
thread the schema is already active).

Signal -> EventType -> recipient mapping table (D3-C-4):

| Source signal                         | EventType                       | Recipient(s)                          |
|---------------------------------------|---------------------------------|---------------------------------------|
| attendance.student_marked_absent      | attendance.absent               | guardians (parents.Guardian) of student|
| academics.grade_changed               | academics.grades_published      | the student + guardians               |
| assignments.assignment_published      | assignments.created             | cohort members (students)             |
| assignments.assignment_due_soon       | assignments.due_soon            | cohort members (students)             |
| assignments.submission_graded         | assignments.graded             | the student + guardians               |
| schedule.lesson_reminder_due          | schedule.lesson_reminder        | cohort members (students)             |
| schedule.lesson_cancelled             | schedule.lesson_reminder        | cohort members (students)             |
| schedule.lesson_rescheduled           | schedule.lesson_reminder        | cohort members (students)             |
| auth.login_succeeded                  | auth.new_device_login           | the logging-in user                   |
| cohorts.cohort_member_moved           | students.enrollment_changed     | the student + guardians               |
| finance.invoice_issued                | finance.invoice_issued          | payer (created_by) + primary guardian |
| finance.payment_reminder              | finance.payment_reminder        | payer + primary guardian              |
| payments.payment_completed            | payments.payment_completed      | payer + primary guardian              |
| payments.payment_failed               | payments.payment_failed         | payer + primary guardian              |

NOTE on event types whose dedicated source signal does not exist yet:
``students.enrollment_changed`` is bridged from the *existing*
``cohorts.cohort_member_moved`` signal (the closest published Day-1 signal —
Lane A's dedicated enrollment signal lands later and can add a second receiver).
``academics.grades_published`` is bridged from the existing
``academics.grade_changed`` signal (fires on result overwrite). Finance/payments
receivers are wired and connected; their signals are emitted by Lanes A/B which
merge before C — the imports are guarded so a not-yet-merged sibling lane does
not break app load.
"""

from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.notifications import services

logger = logging.getLogger("starforge.notifications")


# ---------------------------------------------------------------------------
# Recipient resolution helpers
# ---------------------------------------------------------------------------
def _guardian_user_ids(student_id: int) -> list[int]:
    from apps.parents.models import Guardian

    return list(Guardian.objects.filter(student_id=student_id).values_list("parent__user_id", flat=True))


def _primary_guardian_user_id(student_id: int) -> int | None:
    from apps.parents.models import Guardian

    return (
        Guardian.objects.filter(student_id=student_id, is_primary=True)
        .values_list("parent__user_id", flat=True)
        .first()
    )


def _student_user_id(student_id: int) -> int | None:
    from apps.students.models import StudentProfile

    return StudentProfile.objects.filter(pk=student_id).values_list("user_id", flat=True).first()


def _cohort_member_user_ids(cohort_id: int) -> list[int]:
    from apps.cohorts.models import CohortMembership

    return list(
        CohortMembership.objects.filter(cohort_id=cohort_id, end_date__isnull=True).values_list(
            "student__user_id", flat=True
        )
    )


def _push_cohort_attendance(*, lesson_id: int, payload: dict) -> None:
    """Relay a live attendance update to the lesson's cohort group (D4-LC-6).

    Resolves the cohort from the lesson and emits one schema-prefixed cohort
    group_send via the notifications producer. Best-effort: if the lesson is
    gone or the channel layer is unconfigured, the in-app feed remains the source
    of truth, so a missed frame is non-fatal.
    """
    from apps.schedule.models import Lesson

    cohort_id = Lesson.objects.filter(pk=lesson_id).values_list("cohort_id", flat=True).first()
    if cohort_id is None:
        return
    services.push_cohort_attendance(cohort_id=cohort_id, payload=payload)


# Above this many recipients, a fan-out is offloaded to chunked Celery tasks instead
# of dispatched inline: a cohort-wide event (lesson reschedule/cancel, assignment
# publish) for a large class would otherwise block the triggering HTTP request on
# O(recipients) x ~3-4 queries each. Small fan-outs (a single student's guardians)
# stay inline — low latency, no worker round-trip.
_FANOUT_INLINE_MAX = 25
_FANOUT_CHUNK = 100


def _dispatch_many(*, user_ids, event_type: str, context: dict, dedupe_prefix: str | None = None) -> None:
    unique = list(dict.fromkeys(uid for uid in user_ids if uid is not None))  # de-dupe, preserve order
    if len(unique) <= _FANOUT_INLINE_MAX:
        for uid in unique:
            dedupe_key = f"{dedupe_prefix}:{uid}" if dedupe_prefix else None
            services.dispatch(
                event_type=event_type, recipient_id=uid, context=context, dedupe_key=dedupe_key
            )
        return
    # Large fan-out -> chunked Celery (mirrors announce_cohort); same dedupe contract.
    from celery_tasks.notification_tasks import dispatch_many_chunk
    from core.utils import current_schema

    schema = current_schema()
    for i in range(0, len(unique), _FANOUT_CHUNK):
        dispatch_many_chunk.delay(
            user_ids=unique[i : i + _FANOUT_CHUNK],
            event_type=event_type,
            context=context,
            dedupe_prefix=dedupe_prefix,
            _schema_name=schema,
        )


# ---------------------------------------------------------------------------
# Attendance (D2-B)
# ---------------------------------------------------------------------------
def _connect_attendance() -> None:
    try:
        from apps.attendance.signals import student_marked_absent
    except Exception:  # pragma: no cover - sibling lane not present
        return

    @receiver(student_marked_absent, dispatch_uid="notifications.student_marked_absent", weak=False)
    def on_student_marked_absent(sender, *, record_id, student_id, lesson_id, auto, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        guardians = _guardian_user_ids(student_id)
        context = {"student_id": student_id, "lesson_id": lesson_id, "auto": auto}
        _dispatch_many(
            user_ids=guardians,
            event_type=EventType.ATTENDANCE_ABSENT,
            context=context,
            dedupe_prefix=f"attendance.absent:{record_id}",
        )
        # D4-LC-6: live attendance dashboard update. This receiver fires exactly
        # once per record that becomes absent (manual or sweep), so the cohort
        # group_send is emitted once per event — not once per guardian. dispatch
        # (this notifications stack) is the ONLY group_send producer (TD-15); the
        # cohort group is schema-prefixed inside push_cohort_attendance.
        _push_cohort_attendance(
            lesson_id=lesson_id,
            payload={
                "record_id": record_id,
                "student_id": student_id,
                "lesson_id": lesson_id,
                "status": "absent",
                "auto": auto,
            },
        )


# ---------------------------------------------------------------------------
# Academics (D2-C) — grade_changed bridges to grades_published
# ---------------------------------------------------------------------------
def _connect_academics() -> None:
    try:
        from apps.academics.signals import grade_changed
    except Exception:  # pragma: no cover
        return

    @receiver(grade_changed, dispatch_uid="notifications.grade_changed", weak=False)
    def on_grade_changed(sender, *, instance, old_score, new_score, actor_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        student_id = getattr(instance, "student_id", None)
        if student_id is None:
            return
        recipients = [_student_user_id(student_id), *_guardian_user_ids(student_id)]
        context = {"student_id": student_id, "new_score": str(new_score)}
        # Dedupe per (result, new_score), NOT per result pk: a grade CORRECTION
        # (50->60 then 60->70) is a distinct event the student/parent must hear
        # about. Keying on the row pk alone permanently suppresses every change
        # after the first. The score makes each distinct correction notify once
        # while a double-fire of the same overwrite still collapses.
        _dispatch_many(
            user_ids=recipients,
            event_type=EventType.ACADEMICS_GRADES_PUBLISHED,
            context=context,
            dedupe_prefix=f"academics.grades_published:{getattr(instance, 'pk', '')}:{new_score}",
        )


# ---------------------------------------------------------------------------
# Assignments (D2-D)
# ---------------------------------------------------------------------------
def _connect_assignments() -> None:
    try:
        from apps.assignments.signals import (
            assignment_due_soon,
            assignment_published,
            submission_graded,
        )
    except Exception:  # pragma: no cover
        return

    @receiver(assignment_published, dispatch_uid="notifications.assignment_published", weak=False)
    def on_assignment_published(sender, *, assignment_id, cohort_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_cohort_member_user_ids(cohort_id),
            event_type=EventType.ASSIGNMENTS_CREATED,
            context={"assignment_id": assignment_id, "cohort_id": cohort_id},
            dedupe_prefix=f"assignments.created:{assignment_id}",
        )

    @receiver(assignment_due_soon, dispatch_uid="notifications.assignment_due_soon", weak=False)
    def on_assignment_due_soon(sender, *, assignment_id, cohort_id, due_at, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_cohort_member_user_ids(cohort_id),
            event_type=EventType.ASSIGNMENTS_DUE_SOON,
            context={"assignment_id": assignment_id, "due_at": str(due_at)},
            dedupe_prefix=f"assignments.due_soon:{assignment_id}",
        )

    @receiver(submission_graded, dispatch_uid="notifications.submission_graded", weak=False)
    def on_submission_graded(sender, *, submission_id, student_id, score, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        recipients = [_student_user_id(student_id), *_guardian_user_ids(student_id)]
        # Same correction issue as grade_changed: re-grading a submission
        # (grade_submission uses update_or_create) is a distinct event. Include
        # the score so each new grade notifies once and a double-fire collapses.
        _dispatch_many(
            user_ids=recipients,
            event_type=EventType.ASSIGNMENTS_GRADED,
            context={"submission_id": submission_id, "score": str(score)},
            dedupe_prefix=f"assignments.graded:{submission_id}:{score}",
        )


# ---------------------------------------------------------------------------
# Schedule (D2-A)
# ---------------------------------------------------------------------------
def _connect_schedule() -> None:
    try:
        from apps.schedule.signals import (
            lesson_cancelled,
            lesson_reminder_due,
            lesson_rescheduled,
        )
    except Exception:  # pragma: no cover
        return

    def _lesson_recipients(lesson_id):
        from apps.schedule.models import Lesson

        cohort_id = Lesson.objects.filter(pk=lesson_id).values_list("cohort_id", flat=True).first()
        if cohort_id is None:
            return []
        return _cohort_member_user_ids(cohort_id)

    @receiver(lesson_reminder_due, dispatch_uid="notifications.lesson_reminder_due", weak=False)
    def on_lesson_reminder_due(sender, *, lesson_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_lesson_recipients(lesson_id),
            event_type=EventType.SCHEDULE_LESSON_REMINDER,
            context={"lesson_id": lesson_id, "kind": "reminder"},
            dedupe_prefix=f"schedule.lesson_reminder:{lesson_id}",
        )

    @receiver(lesson_cancelled, dispatch_uid="notifications.lesson_cancelled", weak=False)
    def on_lesson_cancelled(sender, *, lesson_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_lesson_recipients(lesson_id),
            event_type=EventType.SCHEDULE_LESSON_REMINDER,
            context={"lesson_id": lesson_id, "kind": "cancelled"},
            dedupe_prefix=f"schedule.lesson_cancelled:{lesson_id}",
        )

    @receiver(lesson_rescheduled, dispatch_uid="notifications.lesson_rescheduled", weak=False)
    def on_lesson_rescheduled(sender, *, lesson_id, old_start="", moved_at="", schema_name="", **kwargs):
        from apps.notifications.models import EventType

        # dispatch() dedupes permanently on the key, so EVERY distinct move must produce
        # a distinct key or its notification is silently dropped (the anti-pattern
        # on_grade_changed / on_submission_graded avoid). old_start alone re-collides
        # whenever a lesson returns to a previously-occupied slot (A->B->A->B: the 3rd
        # move's old_start=A equals the 1st's). moved_at = the moved lesson's updated_at,
        # bumped on every save, so it is monotonic + unique per move and never repeats.
        _dispatch_many(
            user_ids=_lesson_recipients(lesson_id),
            event_type=EventType.SCHEDULE_LESSON_REMINDER,
            context={"lesson_id": lesson_id, "kind": "rescheduled", "old_start": old_start},
            dedupe_prefix=f"schedule.lesson_rescheduled:{lesson_id}:{moved_at or old_start}",
        )


# ---------------------------------------------------------------------------
# Auth (D1-C) — new device login
# ---------------------------------------------------------------------------
def _connect_auth() -> None:
    try:
        from apps.auth.signals import login_succeeded
    except Exception:  # pragma: no cover
        return

    @receiver(login_succeeded, dispatch_uid="notifications.login_succeeded", weak=False)
    def on_login_succeeded(
        sender, *, username=None, user_id=None, ip="", user_agent="", schema_name="", **kwargs
    ):
        if user_id is None:
            return
        # SUPPRESSED until the login signal carries device info. login_succeeded
        # fires on EVERY successful login; AUTH_NEW_DEVICE_LOGIN defaults ON for
        # in-app + push, so dispatching here cried "New device login" on every
        # routine login — a false security alert. The real fix needs the auth
        # flow to pass a device_id/fingerprint so we can dispatch ONLY when no
        # prior non-revoked Device exists for that device (or dedupe per
        # (user, device)). The receiver stays connected (wiring tested) but
        # no-ops the dispatch until that signal payload lands.
        # TODO(device-novelty): once login_succeeded carries device_id, resolve
        # the device and dispatch EventType.AUTH_NEW_DEVICE_LOGIN only for a
        # genuinely new device, deduped per (user, device).
        return


# ---------------------------------------------------------------------------
# Cohorts (D1-D) — enrollment changed (bridged from member-moved)
# ---------------------------------------------------------------------------
def _connect_cohorts() -> None:
    try:
        from apps.cohorts.signals import cohort_member_moved
    except Exception:  # pragma: no cover
        return

    @receiver(cohort_member_moved, dispatch_uid="notifications.cohort_member_moved", weak=False)
    def on_cohort_member_moved(sender, *, student_id, to_cohort_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        recipients = [_student_user_id(student_id), *_guardian_user_ids(student_id)]
        _dispatch_many(
            user_ids=recipients,
            event_type=EventType.STUDENTS_ENROLLMENT_CHANGED,
            context={"student_id": student_id, "to_cohort_id": to_cohort_id},
        )


# ---------------------------------------------------------------------------
# Finance (D3-A) — invoice issued / payment reminder
# ---------------------------------------------------------------------------
def _invoice_recipients(invoice_id, student_id):
    """Payer (invoice.created_by) + the student's primary guardian."""
    recipients: list[int | None] = []
    try:
        from apps.finance.models import Invoice

        created_by = Invoice.objects.filter(pk=invoice_id).values_list("created_by_id", flat=True).first()
        recipients.append(created_by)
    except Exception:  # pragma: no cover - finance not merged yet
        pass
    recipients.append(_primary_guardian_user_id(student_id))
    return recipients


def _connect_finance() -> None:
    try:
        from apps.finance.signals import invoice_issued, payment_reminder
    except Exception:  # pragma: no cover - Lane A not merged yet
        return

    @receiver(invoice_issued, dispatch_uid="notifications.invoice_issued", weak=False)
    def on_invoice_issued(sender, *, invoice_id, student_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.FINANCE_INVOICE_ISSUED,
            context={"invoice_id": invoice_id, "student_id": student_id},
            dedupe_prefix=f"finance.invoice_issued:{invoice_id}",
        )

    @receiver(payment_reminder, dispatch_uid="notifications.payment_reminder", weak=False)
    def on_payment_reminder(sender, *, invoice_id, student_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        # The finance reminder beat task supplies its own date-bucketed dedupe via
        # the dispatch call site (DAY-3 D3-A-8); here we still namespace per invoice.
        _dispatch_many(
            user_ids=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.FINANCE_PAYMENT_REMINDER,
            context={"invoice_id": invoice_id, "student_id": student_id},
        )


# ---------------------------------------------------------------------------
# Payments (D3-B) — payment completed / failed
# ---------------------------------------------------------------------------
def _connect_payments() -> None:
    try:
        from apps.payments.signals import payment_completed, payment_failed
    except Exception:  # pragma: no cover - Lane B not merged yet
        return

    @receiver(payment_completed, dispatch_uid="notifications.payment_completed", weak=False)
    def on_payment_completed(
        sender, *, payment_id, invoice_id, student_id, amount_uzs, schema_name="", **kwargs
    ):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
            context={"payment_id": payment_id, "amount_uzs": str(amount_uzs)},
            dedupe_prefix=f"payments.payment_completed:{payment_id}",
        )

    @receiver(payment_failed, dispatch_uid="notifications.payment_failed", weak=False)
    def on_payment_failed(
        sender, *, payment_id, invoice_id, student_id, amount_uzs, schema_name="", **kwargs
    ):
        from apps.notifications.models import EventType

        _dispatch_many(
            user_ids=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.PAYMENTS_PAYMENT_FAILED,
            context={"payment_id": payment_id, "amount_uzs": str(amount_uzs)},
            dedupe_prefix=f"payments.payment_failed:{payment_id}",
        )


# Connect everything at import time (apps.ready() imports this module).
_connect_attendance()
_connect_academics()
_connect_assignments()
_connect_schedule()
_connect_auth()
_connect_cohorts()
_connect_finance()
_connect_payments()
