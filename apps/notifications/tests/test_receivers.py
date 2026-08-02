"""Receiver wiring + EventType canonical-list verification (D3-C-2/4).

Per the Day-1 review lesson ("test wiring, not imports"): assert the receivers
are actually CONNECTED to the source signals (not merely importable), and that
the EventType enum matches the DAY-3 D3-C-2 canonical list verbatim.
"""

from __future__ import annotations

import pytest
from django_tenants.utils import schema_context

from apps.notifications.models import Channel, EventType, Notification

pytestmark = pytest.mark.django_db

# The canonical D3-C-2 list (extend, never rename).
CANONICAL_EVENT_TYPES = {
    "attendance.absent",
    "attendance.late",
    "academics.grades_published",
    "assignments.created",
    "assignments.due_soon",
    "assignments.graded",
    "schedule.lesson_reminder",
    "auth.new_device_login",
    "students.enrollment_changed",
    "finance.invoice_issued",
    "finance.payment_reminder",
    "payments.payment_completed",
    "payments.payment_failed",
    "cohorts.announcement",
    "billing.subscription_past_due",
    "billing.subscription_suspended",
}


def test_event_type_covers_canonical_list():
    values = {choice for choice, _label in EventType.choices}
    # extend-never-rename: every canonical value must be present.
    assert values >= CANONICAL_EVENT_TYPES


@pytest.mark.parametrize(
    "event_type",
    [
        EventType.COVER_REQUESTED,
        EventType.COVER_APPROVED,
        EventType.COVER_POOL_OPENED,
        EventType.COVER_REJECTED,
    ],
)
def test_cover_events_render_localized_template_and_preserve_context(tenant_a, event_type):
    from apps.notifications.services import dispatch
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = UserFactory(preferred_language="en")
        context = {"cover_id": 17, "lesson_id": 29}
        notification = dispatch(
            event_type=event_type,
            recipient_id=user.pk,
            context=context,
            channels=[Channel.IN_APP],
        )
        assert notification is not None
        assert notification.event_type == event_type
        assert notification.data == context
        assert notification.title
        assert notification.body


def _connected_uids(signal) -> list[str]:
    """The dispatch_uid of each connected receiver.

    Django 6 stores each receiver as a tuple whose first element is the
    ``lookup_key`` ``(dispatch_uid_or_id, sender_id)`` — ``lookup_key[0]`` is the
    dispatch_uid string when one was supplied.
    """
    return [str(entry[0][0]) for entry in signal.receivers]


def test_attendance_signal_receiver_connected():
    """Both attendance transition signals have notification receivers."""
    from apps.attendance.signals import student_marked_absent, student_marked_late

    assert any("notifications.student_marked_absent" in uid for uid in _connected_uids(student_marked_absent))
    assert any("notifications.student_marked_late" in uid for uid in _connected_uids(student_marked_late))


def test_student_marked_late_notifies_guardian_with_context(tenant_a):
    from apps.attendance.signals import student_marked_late
    from apps.parents.tests.factories import GuardianFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        guardian = GuardianFactory(student=student)
        student_marked_late.send(
            sender=None,
            record_id=31,
            student_id=student.pk,
            lesson_id=47,
            schema_name=tenant_a.schema_name,
        )
        notification = Notification.objects.get(
            user=guardian.parent.user,
            event_type=EventType.ATTENDANCE_LATE,
        )
        assert notification.data == {"student_id": student.pk, "lesson_id": 47}


def test_assignment_published_receiver_connected():
    from apps.assignments.signals import assignment_published

    assert any("notifications.assignment_published" in uid for uid in _connected_uids(assignment_published))


def test_auth_login_receiver_connected():
    from apps.auth.signals import login_succeeded

    assert any("notifications.login_succeeded" in uid for uid in _connected_uids(login_succeeded))


def test_cohort_member_moved_bridges_enrollment_changed(tenant_a):
    """cohorts.cohort_member_moved -> students.enrollment_changed notification."""
    from apps.cohorts.signals import cohort_member_moved
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        cohort_member_moved.send(
            sender=None,
            student_id=student.pk,
            to_cohort_id=7,
            schema_name=tenant_a.schema_name,
        )
        notif = Notification.objects.filter(user=student.user).first()
        assert notif is not None
        assert notif.event_type == EventType.STUDENTS_ENROLLMENT_CHANGED


# ---------------------------------------------------------------------------
# Grade CORRECTIONS must re-notify (dedupe must reflect the change, not the row)
# ---------------------------------------------------------------------------
def test_grade_correction_after_first_change_re_notifies(tenant_a):
    """grade_changed for the SAME ExamResult with a NEW score is a distinct event
    (a correction) and must produce a SECOND notification — keying dedupe on the
    row pk alone permanently suppressed every correction after the first."""
    from decimal import Decimal

    from apps.academics.signals import grade_changed
    from apps.academics.tests.factories import ExamResultFactory

    with schema_context(tenant_a.schema_name):
        result = ExamResultFactory(score=Decimal("60"))
        student = result.student

        grade_changed.send(
            sender=type(result),
            instance=result,
            old_score=Decimal("50"),
            new_score=Decimal("60"),
            actor_id=None,
            schema_name=tenant_a.schema_name,
        )
        grade_changed.send(
            sender=type(result),
            instance=result,
            old_score=Decimal("60"),
            new_score=Decimal("70"),
            actor_id=None,
            schema_name=tenant_a.schema_name,
        )

        notifs = Notification.objects.filter(
            user=student.user, event_type=EventType.ACADEMICS_GRADES_PUBLISHED
        )
        # Two distinct corrections -> two notifications.
        assert notifs.count() == 2


def test_grade_same_score_double_fire_still_dedupes(tenant_a):
    """Control: a double-fire of the SAME (result, score) still collapses to one."""
    from decimal import Decimal

    from apps.academics.signals import grade_changed
    from apps.academics.tests.factories import ExamResultFactory

    with schema_context(tenant_a.schema_name):
        result = ExamResultFactory(score=Decimal("90"))
        student = result.student
        for _ in range(2):
            grade_changed.send(
                sender=type(result),
                instance=result,
                old_score=Decimal("80"),
                new_score=Decimal("90"),
                actor_id=None,
                schema_name=tenant_a.schema_name,
            )
        assert (
            Notification.objects.filter(
                user=student.user, event_type=EventType.ACADEMICS_GRADES_PUBLISHED
            ).count()
            == 1
        )


# ---------------------------------------------------------------------------
# auth.new_device_login must NOT fire on every login (false security alert)
# ---------------------------------------------------------------------------
def test_login_succeeded_does_not_dispatch_new_device_login(tenant_a):
    """The receiver stays CONNECTED (wiring under test) but must NOT dispatch a
    'New device login' on every routine login until the signal carries device
    info — that cried wolf on each login."""
    from apps.auth.signals import login_succeeded
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = UserFactory()
        login_succeeded.send(
            sender=None,
            username=user.username,
            user_id=user.pk,
            ip="1.2.3.4",
            user_agent="ua",
            schema_name=tenant_a.schema_name,
        )
        assert not Notification.objects.filter(user=user, event_type=EventType.AUTH_NEW_DEVICE_LOGIN).exists()


def test_login_succeeded_receiver_still_connected():
    """Suppressing the dispatch must NOT disconnect the receiver (test wiring)."""
    from apps.auth.signals import login_succeeded

    assert any("notifications.login_succeeded" in uid for uid in _connected_uids(login_succeeded))


def test_new_device_signal_dispatches_once_without_persisting_device_id(tenant_a):
    from apps.auth.signals import login_succeeded
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        user = UserFactory()
        payload = {
            "sender": None,
            "username": user.username,
            "user_id": user.pk,
            "ip": "1.2.3.4",
            "user_agent": "browser",
            "device_id": "private-device-id",
            "is_new_device": True,
            "schema_name": tenant_a.schema_name,
        }
        login_succeeded.send(**payload)
        login_succeeded.send(**payload)

        notification = Notification.objects.get(
            user=user,
            event_type=EventType.AUTH_NEW_DEVICE_LOGIN,
        )
        assert notification.data == {"ip": "1.2.3.4", "user_agent": "browser"}
        assert "private-device-id" not in notification.dedupe_key


def test_role_login_produces_only_first_login_for_each_device(tenant_a, client_for, user_in):
    password = "Quasar-Lantern-42"
    user = user_in(tenant_a, roles=["teacher"])
    with schema_context(tenant_a.schema_name):
        teacher = user.teacher_profile
        teacher.set_password(password)
        teacher.save(update_fields=["password"])

    client = client_for(tenant_a)
    payload = {
        "username": teacher.username,
        "password": password,
        "device_id": "phone-1",
        "platform": "android",
    }
    assert client.post("/api/v1/auth/role-login/", payload, format="json").status_code == 200
    assert client.post("/api/v1/auth/role-login/", payload, format="json").status_code == 200

    with schema_context(tenant_a.schema_name):
        assert (
            Notification.objects.filter(
                user=user,
                event_type=EventType.AUTH_NEW_DEVICE_LOGIN,
            ).count()
            == 1
        )


def test_role_login_produces_new_device_notification(tenant_a, client_for):
    from apps.students.tests.factories import StudentProfileFactory

    password = "Quasar-Lantern-42"
    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory(username="device.student")
        student.set_password(password)
        student.save(update_fields=["password"])

    response = client_for(tenant_a).post(
        "/api/v1/auth/role-login/",
        {
            "username": student.username,
            "password": password,
            "device_id": "tablet-1",
            "platform": "android",
        },
        format="json",
    )
    assert response.status_code == 200, response.content
    with schema_context(tenant_a.schema_name):
        assert (
            Notification.objects.filter(
                user=student.user,
                event_type=EventType.AUTH_NEW_DEVICE_LOGIN,
            ).count()
            == 1
        )


def test_payment_reminder_is_deduped_per_producer_cycle(tenant_a, monkeypatch):
    from apps.finance.signals import payment_reminder
    from apps.notifications import receivers
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant_a.schema_name):
        recipient = UserFactory()
        monkeypatch.setattr(receivers, "_invoice_recipients", lambda invoice_id, student_id: [recipient.pk])
        payload = {
            "sender": None,
            "invoice_id": 11,
            "student_id": 22,
            "reminder_cycle": "2026-07-01:3:4",
            "schema_name": tenant_a.schema_name,
        }
        payment_reminder.send(**payload)
        payment_reminder.send(**payload)

        rows = Notification.objects.filter(
            user=recipient,
            event_type=EventType.FINANCE_PAYMENT_REMINDER,
        )
        assert rows.count() == 1
        assert rows.get().data["reminder_cycle"] == "2026-07-01:3:4"

        # A later producer bucket is a distinct reminder; replaying either bucket
        # remains idempotent regardless of the receiver's wall-clock date.
        payment_reminder.send(**{**payload, "reminder_cycle": "2026-07-01:3:5"})
        assert rows.count() == 2
