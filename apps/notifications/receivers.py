"""Notification receivers — the single bridge from domain signals to dispatch().

Connected in ``NotificationsConfig.ready()``. Domain apps emit emit-only signals
(inside ``transaction.on_commit``); ordinary low-cardinality receivers resolve the
recipient(s) and call ``services.dispatch()`` once per recipient. High-cardinality
bulk schedule events enqueue one tenant-scoped coordinator instead, keeping the
request commit path out of the lessons x recipients fan-out. Each receiver runs
inside the emitting tenant's schema and every asynchronous hop carries that schema.

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
| schedule.lessons_bulk_rescheduled     | schedule.lesson_reminder        | cohort members (async, bounded)       |
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
from django.utils import timezone

from apps.notifications import services
from core.utils import stable_hash

logger = logging.getLogger("starforge.notifications")


# ---------------------------------------------------------------------------
# Recipient resolution helpers
# ---------------------------------------------------------------------------
def _guardian_recipients(student_id: int) -> list[dict]:
    from apps.parents.models import Guardian

    return [
        {"user_id": user_id, "principal_kind": "parent", "principal_id": parent_id}
        for user_id, parent_id in Guardian.objects.filter(
            student_id=student_id,
            revoked_at__isnull=True,
            parent__is_active=True,
            parent__user__is_active=True,
        ).values_list("parent__user_id", "parent_id")
    ]


def _primary_guardian_recipient(student_id: int) -> dict | None:
    from apps.parents.models import Guardian

    row = (
        Guardian.objects.filter(
            student_id=student_id,
            is_primary=True,
            revoked_at__isnull=True,
            parent__is_active=True,
            parent__user__is_active=True,
        )
        .values_list("parent__user_id", "parent_id")
        .first()
    )
    if row is None:
        return None
    return {"user_id": row[0], "principal_kind": "parent", "principal_id": row[1]}


def _student_recipient(student_id: int) -> dict | None:
    from apps.students.models import StudentProfile

    user_id = (
        StudentProfile.objects.filter(pk=student_id, is_active=True, user__is_active=True)
        .values_list("user_id", flat=True)
        .first()
    )
    if user_id is None:
        return None
    return {"user_id": user_id, "principal_kind": "student", "principal_id": student_id}


def _cohort_member_recipients(cohort_id: int) -> list[dict]:
    from apps.cohorts.models import CohortMembership

    return [
        {"user_id": user_id, "principal_kind": "student", "principal_id": student_id}
        for user_id, student_id in CohortMembership.objects.filter(
            cohort_id=cohort_id,
            end_date__isnull=True,
            student__is_active=True,
            student__user__is_active=True,
        ).values_list("student__user_id", "student_id")
    ]


def _staff_recipient(user_id: int | None) -> dict | None:
    if user_id is None:
        return None
    from apps.org.models import StaffProfile

    profile_id = (
        StaffProfile.objects.filter(user_id=user_id, is_active=True).values_list("pk", flat=True).first()
    )
    if profile_id is None:
        # Preserve the row as quarantine evidence; dispatch's conservative
        # resolver may still prove a different single role, but never guesses
        # that a finance/approval actor is staff merely from a bridge User id.
        return {"user_id": user_id}
    return {"user_id": user_id, "principal_kind": "staff", "principal_id": profile_id}


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


def _recipient_descriptor(value) -> dict | None:
    """Normalize an exact descriptor or a legacy bridge recipient safely.

    Old queued jobs and a few internal callers supplied a User id/object.  Keep
    that compatibility boundary narrow: an id is promoted only when durable
    evidence proves one role principal.  Ambiguous/missing evidence stays an
    unattributed descriptor so ``dispatch`` quarantines it instead of sending
    to every role sharing the bridge account.
    """

    if isinstance(value, dict):
        user_id = value.get("user_id")
        principal_kind = value.get("principal_kind")
        principal_id = value.get("principal_id")
    else:
        user_id = (
            value if isinstance(value, int) and not isinstance(value, bool) else getattr(value, "pk", None)
        )
        principal_kind = None
        principal_id = None
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None
    if principal_kind is not None or principal_id is not None:
        return {
            "user_id": user_id,
            "principal_kind": principal_kind,
            "principal_id": principal_id,
        }

    from apps.notifications.principals import resolve_recipient_principal

    principal = resolve_recipient_principal(user_id=user_id)
    descriptor: dict[str, object] = {"user_id": user_id}
    if principal.is_deliverable and principal.kind is not None and principal.principal_id is not None:
        descriptor.update(
            principal_kind=principal.kind,
            principal_id=principal.principal_id,
        )
    return descriptor


def _dispatch_many(
    *,
    recipients=None,
    user_ids=None,
    event_type: str,
    context: dict,
    dedupe_prefix: str | None = None,
) -> None:
    if recipients is not None and user_ids is not None:
        raise ValueError("Provide recipients or legacy user_ids, not both.")
    unique: list[dict] = []
    seen: set[tuple[object, object, object]] = set()
    for value in recipients if recipients is not None else (user_ids or []):
        recipient = _recipient_descriptor(value)
        if recipient is None:
            continue
        key = (
            recipient.get("user_id"),
            recipient.get("principal_kind"),
            recipient.get("principal_id"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(recipient)
    if len(unique) <= _FANOUT_INLINE_MAX:
        for recipient in unique:
            uid = recipient["user_id"]
            dedupe_key = f"{dedupe_prefix}:{uid}" if dedupe_prefix else None
            services.dispatch(
                event_type=event_type,
                recipient_id=uid,
                recipient_principal_kind=recipient.get("principal_kind"),
                recipient_principal_id=recipient.get("principal_id"),
                context=context,
                dedupe_key=dedupe_key,
            )
        return
    # Large fan-out -> chunked Celery (mirrors announce_cohort); same dedupe contract.
    from celery_tasks.notification_tasks import dispatch_many_chunk
    from core.utils import current_schema

    schema = current_schema()
    for i in range(0, len(unique), _FANOUT_CHUNK):
        dispatch_many_chunk.delay(
            recipients=unique[i : i + _FANOUT_CHUNK],
            event_type=event_type,
            context=context,
            dedupe_prefix=dedupe_prefix,
            _schema_name=schema,
        )


# ---------------------------------------------------------------------------
# Attendance (D2-B)
# ---------------------------------------------------------------------------
def _connect_attendance() -> None:
    from apps.attendance.signals import student_marked_absent, student_marked_late

    @receiver(student_marked_absent, dispatch_uid="notifications.student_marked_absent", weak=False)
    def on_student_marked_absent(sender, *, record_id, student_id, lesson_id, auto, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        guardians = _guardian_recipients(student_id)
        context = {"student_id": student_id, "lesson_id": lesson_id, "auto": auto}
        _dispatch_many(
            recipients=guardians,
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

    @receiver(student_marked_late, dispatch_uid="notifications.student_marked_late", weak=False)
    def on_student_marked_late(sender, *, record_id, student_id, lesson_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_guardian_recipients(student_id),
            event_type=EventType.ATTENDANCE_LATE,
            context={"student_id": student_id, "lesson_id": lesson_id},
            dedupe_prefix=f"attendance.late:{record_id}",
        )
        _push_cohort_attendance(
            lesson_id=lesson_id,
            payload={
                "record_id": record_id,
                "student_id": student_id,
                "lesson_id": lesson_id,
                "status": "late",
                "auto": False,
            },
        )


# ---------------------------------------------------------------------------
# Academics (D2-C) — grade_changed bridges to grades_published
# ---------------------------------------------------------------------------
def _connect_academics() -> None:
    from apps.academics.signals import grade_changed

    @receiver(grade_changed, dispatch_uid="notifications.grade_changed", weak=False)
    def on_grade_changed(sender, *, instance, old_score, new_score, actor_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        student_id = getattr(instance, "student_id", None)
        if student_id is None:
            return
        recipients = [_student_recipient(student_id), *_guardian_recipients(student_id)]
        context = {"student_id": student_id, "new_score": str(new_score)}
        # Dedupe per (result, new_score), NOT per result pk: a grade CORRECTION
        # (50->60 then 60->70) is a distinct event the student/parent must hear
        # about. Keying on the row pk alone permanently suppresses every change
        # after the first. The score makes each distinct correction notify once
        # while a double-fire of the same overwrite still collapses.
        _dispatch_many(
            recipients=recipients,
            event_type=EventType.ACADEMICS_GRADES_PUBLISHED,
            context=context,
            dedupe_prefix=f"academics.grades_published:{getattr(instance, 'pk', '')}:{new_score}",
        )


# ---------------------------------------------------------------------------
# Assignments (D2-D)
# ---------------------------------------------------------------------------
def _connect_assignments() -> None:
    from apps.assignments.signals import (
        assignment_due_soon,
        assignment_published,
        submission_graded,
    )

    @receiver(assignment_published, dispatch_uid="notifications.assignment_published", weak=False)
    def on_assignment_published(sender, *, assignment_id, cohort_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_cohort_member_recipients(cohort_id),
            event_type=EventType.ASSIGNMENTS_CREATED,
            context={"assignment_id": assignment_id, "cohort_id": cohort_id},
            dedupe_prefix=f"assignments.created:{assignment_id}",
        )

    @receiver(assignment_due_soon, dispatch_uid="notifications.assignment_due_soon", weak=False)
    def on_assignment_due_soon(sender, *, assignment_id, cohort_id, due_at, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_cohort_member_recipients(cohort_id),
            event_type=EventType.ASSIGNMENTS_DUE_SOON,
            context={"assignment_id": assignment_id, "due_at": str(due_at)},
            dedupe_prefix=f"assignments.due_soon:{assignment_id}",
        )

    @receiver(submission_graded, dispatch_uid="notifications.submission_graded", weak=False)
    def on_submission_graded(sender, *, submission_id, student_id, score, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        recipients = [_student_recipient(student_id), *_guardian_recipients(student_id)]
        # Same correction issue as grade_changed: re-grading a submission
        # (grade_submission uses update_or_create) is a distinct event. Include
        # the score so each new grade notifies once and a double-fire collapses.
        _dispatch_many(
            recipients=recipients,
            event_type=EventType.ASSIGNMENTS_GRADED,
            context={
                "submission_id": submission_id,
                "student_id": student_id,
                "score": str(score),
            },
            dedupe_prefix=f"assignments.graded:{submission_id}:{score}",
        )


# ---------------------------------------------------------------------------
# Schedule (D2-A)
# ---------------------------------------------------------------------------
def _connect_schedule() -> None:
    from apps.schedule.signals import (
        lesson_cancelled,
        lesson_reminder_due,
        lesson_rescheduled,
        lessons_bulk_rescheduled,
    )

    def _lesson_recipients(lesson_id):
        from apps.schedule.models import Lesson

        cohort_id = Lesson.objects.filter(pk=lesson_id).values_list("cohort_id", flat=True).first()
        if cohort_id is None:
            return []
        return _cohort_member_recipients(cohort_id)

    @receiver(lesson_reminder_due, dispatch_uid="notifications.lesson_reminder_due", weak=False)
    def on_lesson_reminder_due(sender, *, lesson_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_lesson_recipients(lesson_id),
            event_type=EventType.SCHEDULE_LESSON_REMINDER,
            context={"lesson_id": lesson_id, "kind": "reminder"},
            dedupe_prefix=f"schedule.lesson_reminder:{lesson_id}",
        )

    @receiver(lesson_cancelled, dispatch_uid="notifications.lesson_cancelled", weak=False)
    def on_lesson_cancelled(sender, *, lesson_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_lesson_recipients(lesson_id),
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
            recipients=_lesson_recipients(lesson_id),
            event_type=EventType.SCHEDULE_LESSON_REMINDER,
            context={"lesson_id": lesson_id, "kind": "rescheduled", "old_start": old_start},
            dedupe_prefix=f"schedule.lesson_rescheduled:{lesson_id}:{moved_at or old_start}",
        )

    @receiver(
        lessons_bulk_rescheduled,
        dispatch_uid="notifications.lessons_bulk_rescheduled",
        weak=False,
    )
    def on_lessons_bulk_rescheduled(
        sender,
        *,
        cohort_id,
        moves,
        schema_name="",
        **kwargs,
    ):
        """Queue one coordinator; never resolve recipients on the request thread.

        The coordinator streams the cohort once and publishes bounded child jobs.
        Supplying ``schema_name`` explicitly preserves the tenant when the worker
        executes after this request's schema context has gone away.
        """
        from celery_tasks.notification_tasks import coordinate_lesson_reschedule_fanout
        from core.utils import current_schema

        coordinate_lesson_reschedule_fanout.delay(
            cohort_id=cohort_id,
            moves=list(moves),
            _schema_name=schema_name or current_schema(),
        )


# ---------------------------------------------------------------------------
# Auth (D1-C) — new device login
# ---------------------------------------------------------------------------
def _connect_auth() -> None:
    from apps.auth.signals import login_succeeded

    @receiver(login_succeeded, dispatch_uid="notifications.login_succeeded", weak=False)
    def on_login_succeeded(
        sender,
        *,
        username=None,
        user_id=None,
        ip="",
        user_agent="",
        device_id="",
        is_new_device=False,
        principal_kind="",
        principal_id=None,
        schema_name="",
        **kwargs,
    ):
        if user_id is None or not device_id or not is_new_device:
            return
        from apps.notifications.models import EventType

        # Keep the client-controlled identifier out of the unique key and
        # persisted payload. Its stable digest remains bounded and makes a
        # retried signal idempotent for the same device.
        device_fingerprint = stable_hash(device_id)
        services.dispatch(
            event_type=EventType.AUTH_NEW_DEVICE_LOGIN,
            recipient_id=user_id,
            recipient_principal_kind=principal_kind,
            recipient_principal_id=principal_id,
            context={"ip": ip, "user_agent": user_agent},
            dedupe_key=f"auth.new_device_login:{user_id}:{device_fingerprint}",
        )


# ---------------------------------------------------------------------------
# Cohorts (D1-D) — enrollment changed (bridged from member-moved)
# ---------------------------------------------------------------------------
def _connect_cohorts() -> None:
    from apps.cohorts.signals import cohort_member_moved

    @receiver(cohort_member_moved, dispatch_uid="notifications.cohort_member_moved", weak=False)
    def on_cohort_member_moved(sender, *, student_id, to_cohort_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        recipients = [_student_recipient(student_id), *_guardian_recipients(student_id)]
        _dispatch_many(
            recipients=recipients,
            event_type=EventType.STUDENTS_ENROLLMENT_CHANGED,
            context={"student_id": student_id, "to_cohort_id": to_cohort_id},
        )


# ---------------------------------------------------------------------------
# Finance (D3-A) — invoice issued / payment reminder
# ---------------------------------------------------------------------------
def _invoice_recipients(invoice_id, student_id):
    """Payer (invoice.created_by) + the student's primary guardian."""
    from apps.finance.models import Invoice

    recipients: list[dict | None] = []
    created_by = Invoice.objects.filter(pk=invoice_id).values_list("created_by_id", flat=True).first()
    recipients.append(_staff_recipient(created_by))
    recipients.append(_primary_guardian_recipient(student_id))
    return recipients


def _connect_finance() -> None:
    from apps.finance.signals import invoice_issued, payment_reminder

    @receiver(invoice_issued, dispatch_uid="notifications.invoice_issued", weak=False)
    def on_invoice_issued(sender, *, invoice_id, student_id, schema_name="", **kwargs):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.FINANCE_INVOICE_ISSUED,
            context={"invoice_id": invoice_id, "student_id": student_id},
            dedupe_prefix=f"finance.invoice_issued:{invoice_id}",
        )

    @receiver(payment_reminder, dispatch_uid="notifications.payment_reminder", weak=False)
    def on_payment_reminder(
        sender,
        *,
        invoice_id,
        student_id,
        reminder_cycle="",
        reminder_date="",
        schema_name="",
        **kwargs,
    ):
        from apps.notifications.models import EventType

        # New producers supply their canonical interval bucket.  Keep the date
        # fallback for old queued events during deployment, but never recompute a
        # producer bucket when it is present.
        cycle = reminder_cycle or reminder_date or timezone.localdate().isoformat()
        _dispatch_many(
            recipients=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.FINANCE_PAYMENT_REMINDER,
            context={
                "invoice_id": invoice_id,
                "student_id": student_id,
                "reminder_cycle": cycle,
            },
            dedupe_prefix=f"finance.payment_reminder:{invoice_id}:{cycle}",
        )


# ---------------------------------------------------------------------------
# Payments (D3-B) — payment completed / failed
# ---------------------------------------------------------------------------
def _connect_payments() -> None:
    from apps.payments.signals import payment_completed, payment_failed

    @receiver(payment_completed, dispatch_uid="notifications.payment_completed", weak=False)
    def on_payment_completed(
        sender, *, payment_id, invoice_id, student_id, amount_uzs, schema_name="", **kwargs
    ):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
            context={
                "payment_id": payment_id,
                "student_id": student_id,
                "amount_uzs": str(amount_uzs),
            },
            dedupe_prefix=f"payments.payment_completed:{payment_id}",
        )

    @receiver(payment_failed, dispatch_uid="notifications.payment_failed", weak=False)
    def on_payment_failed(
        sender, *, payment_id, invoice_id, student_id, amount_uzs, schema_name="", **kwargs
    ):
        from apps.notifications.models import EventType

        _dispatch_many(
            recipients=_invoice_recipients(invoice_id, student_id),
            event_type=EventType.PAYMENTS_PAYMENT_FAILED,
            context={
                "payment_id": payment_id,
                "student_id": student_id,
                "amount_uzs": str(amount_uzs),
            },
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
