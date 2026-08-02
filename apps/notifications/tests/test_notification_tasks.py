"""Regression tests for the notification Celery fan-out (celery_tasks/notification_tasks).

Covers the verified bugs in the messaging cluster:
- in-app/WS group name is SCHEMA-prefixed (cross-tenant leak on shared Redis).
- quiet-hours deferral is idempotent under Celery at-least-once redelivery of
  ``dispatch_notification`` (no double SMS/push), and ``deliver_single_channel``
  no-ops on a second deferred run.
"""

from __future__ import annotations

from datetime import time, timedelta

import pytest
import time_machine
from django.test import override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


def test_push_device_history_has_targeted_expression_index():
    from apps.notifications.models import NotificationDelivery

    indexes = {index.name: index for index in NotificationDelivery._meta.indexes}
    index = indexes["notif_push_device_created_idx"]
    assert index.include == ("notification", "status")
    assert len(index.expressions) == 2


@pytest.mark.django_db(transaction=True)
def test_concurrent_delivery_workers_serialize_on_notification_row(tenant_a, monkeypatch):
    """Duplicate workers cannot both emit the same non-push channel."""
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event

    from django.db import close_old_connections

    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, Notification, NotificationDelivery

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        notification = Notification.objects.create(
            user=user,
            event_type="attendance.absent",
            title="Absent",
            body="A learner is absent.",
        )
        notification_id = notification.pk

    first_holds_lock = Event()
    release_first = Event()
    second_started = Event()
    delivery_calls: list[int] = []

    def slow_delivery(notification, channel, _context, _render_template):
        delivery_calls.append(notification.pk)
        nt._record(notification, channel, NotificationDelivery.Status.SENT)
        first_holds_lock.set()
        if not release_first.wait(timeout=10):
            raise RuntimeError("test timed out waiting to release the first delivery")
        return "sent"

    monkeypatch.setattr(nt, "_deliver", slow_delivery)

    def deliver(*, mark_started: bool = False):
        close_old_connections()
        try:
            with schema_context(tenant_a.schema_name):
                if mark_started:
                    second_started.set()
                return nt.dispatch_notification.run(
                    notification_id,
                    channels=[Channel.IN_APP],
                )
        finally:
            close_old_connections()

    second = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deliver)
        try:
            assert first_holds_lock.wait(timeout=10)
            second = pool.submit(deliver, mark_started=True)
            assert second_started.wait(timeout=10)
            # It has entered the task but is blocked at Notification.select_for_update.
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.25)
        finally:
            release_first.set()
        assert second is not None
        assert first.result(timeout=10)["results"][Channel.IN_APP] == "sent"
        assert second.result(timeout=10)["results"][Channel.IN_APP] == "already_handled"

    assert delivery_calls == [notification_id]
    with schema_context(tenant_a.schema_name):
        assert (
            NotificationDelivery.objects.filter(
                notification_id=notification_id,
                channel=Channel.IN_APP,
                status=NotificationDelivery.Status.SENT,
            ).count()
            == 1
        )
        Notification.objects.filter(pk=notification_id).delete()
        type(user).objects.filter(pk=user.pk).delete()


def test_bulk_reschedule_child_is_exact_principal_and_idempotent_in_database(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications import services
    from apps.notifications.models import EventType, Notification
    from apps.students.tests.factories import StudentProfileFactory

    # Keep this focused on durable notification creation. Channel delivery has
    # separate task tests and must not add external-adapter work to this assertion.
    monkeypatch.setattr(services, "_queue_dispatch", lambda *_args, **_kwargs: None)
    moves = [
        {
            "lesson_id": 7001,
            "old_start": "2026-08-01T09:00:00+05:00",
            "moved_at": "2026-08-01T10:00:00.000001+05:00",
            "move_id": "10000000000000000000000000000001",
        },
        {
            "lesson_id": 7001,
            "old_start": "2026-08-08T09:00:00+05:00",
            "moved_at": "2026-08-01T10:00:00.000002+05:00",
            "move_id": "10000000000000000000000000000002",
        },
    ]
    with schema_context(tenant_a.schema_name):
        students = [StudentProfileFactory(), StudentProfileFactory()]
        recipients = [
            {
                "user_id": student.user_id,
                "principal_kind": "student",
                "principal_id": student.pk,
            }
            for student in students
        ]

        first = nt.dispatch_lesson_reschedule_chunk.run(moves=moves, recipients=recipients)
        second = nt.dispatch_lesson_reschedule_chunk.run(moves=moves, recipients=recipients)

        assert first == second == {"attempted": 4, "deliverable": 4}
        rows = Notification.objects.filter(
            event_type=EventType.SCHEDULE_LESSON_REMINDER,
            dedupe_key__startswith="schedule.lesson_rescheduled:7001:",
        )
        assert rows.count() == 4
        assert set(rows.values_list("recipient_principal_kind", flat=True)) == {"student"}
        assert set(rows.values_list("recipient_principal_id", flat=True)) == {
            student.pk for student in students
        }


def _user_with_phone(tenant):
    from apps.notifications.tests.helpers import ensure_notification_principal
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        user = UserFactory(phone="+998901112233", email="payer@example.com")
        return ensure_notification_principal(user)


def _set_quiet_hours(tenant, *, start, end):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.quiet_hours_start = start
        cs.quiet_hours_end = end
        cs.save(update_fields=["quiet_hours_start", "quiet_hours_end"])


def test_async_fanout_requires_a_stable_dedupe_discriminator():
    import celery_tasks.notification_tasks as nt

    with pytest.raises(ValueError, match="dedupe_prefix"):
        nt.dispatch_many_chunk(
            event_type="assignments.created",
            context={},
            recipients=[],
            dedupe_prefix=None,
        )


@pytest.mark.parametrize(
    ("channel", "setting_name"),
    [
        ("sms", "SMS_ENABLED"),
        ("email", "EMAIL_ENABLED"),
        ("push", "PUSH_NOTIFICATIONS_ENABLED"),
    ],
)
def test_operator_disabled_channel_is_truthfully_skipped_and_idempotent(
    tenant_a, monkeypatch, channel, setting_name
):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Notification, NotificationDelivery

    monkeypatch.setattr(
        nt,
        "_deliver",
        lambda *_args, **_kwargs: pytest.fail("an operator-disabled channel reached its adapter"),
    )
    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        notification = Notification.objects.create(
            user=user,
            event_type="attendance.absent",
            title="Absent",
            body="A learner is absent.",
        )
        with override_settings(**{setting_name: False}):
            first = nt.dispatch_notification(notification.pk, channels=[channel])
            second = nt.dispatch_notification(notification.pk, channels=[channel])

        assert first["results"][channel] == "skipped_disabled"
        assert second["results"][channel] == "already_handled"
        delivery = NotificationDelivery.objects.get(notification=notification, channel=channel)
        assert delivery.status == NotificationDelivery.Status.SKIPPED_DISABLED
        assert delivery.provider_response == {"reason": "operator_disabled"}


@override_settings(SMS_ENABLED=False, EMAIL_ENABLED=False, PUSH_NOTIFICATIONS_ENABLED=False)
def test_all_external_channels_disabled_preserves_in_app_delivery(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, Notification, NotificationDelivery

    pushed: list[int] = []
    monkeypatch.setattr(
        "apps.notifications.services.push_in_app",
        lambda notification, _title, _body: pushed.append(notification.pk),
    )
    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        notification = Notification.objects.create(
            user=user,
            event_type="attendance.absent",
            title="Absent",
            body="A learner is absent.",
        )
        result = nt.dispatch_notification(notification.pk, channels=[Channel.IN_APP])

        assert result["results"][Channel.IN_APP] == "sent"
        assert (
            NotificationDelivery.objects.get(notification=notification, channel=Channel.IN_APP).status
            == NotificationDelivery.Status.SENT
        )
        assert pushed == [notification.pk]


@override_settings(SMS_ENABLED=False)
def test_deferred_delivery_rechecks_operator_channel_switch(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, Notification, NotificationDelivery

    monkeypatch.setattr(
        nt,
        "_deliver",
        lambda *_args, **_kwargs: pytest.fail("a stale deferred task reached its adapter"),
    )
    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        notification = Notification.objects.create(
            user=user,
            event_type="attendance.absent",
            title="Absent",
            body="A learner is absent.",
        )
        NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
        )

        assert nt.deliver_single_channel(notification.pk, Channel.SMS) == "skipped_disabled"
        assert not NotificationDelivery.objects.filter(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
        ).exists()
        assert (
            NotificationDelivery.objects.filter(
                notification=notification,
                channel=Channel.SMS,
                status=NotificationDelivery.Status.SKIPPED_DISABLED,
            ).count()
            == 1
        )


def test_deferred_delivery_rechecks_role_principal_preference(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, Notification, NotificationDelivery
    from apps.notifications.services import upsert_preferences

    monkeypatch.setattr(
        nt,
        "_deliver",
        lambda *_args, **_kwargs: pytest.fail("an opted-out deferred task reached its adapter"),
    )
    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        notification = Notification.objects.create(
            user=user,
            event_type="attendance.absent",
            title="Absent",
            body="A learner is absent.",
        )
        NotificationDelivery.objects.create(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
        )
        upsert_preferences(
            user=user,
            recipient_principal_kind=user.notification_principal_kind,
            recipient_principal_id=user.notification_principal_id,
            rows=[
                {
                    "event_type": notification.event_type,
                    "channel": Channel.SMS,
                    "enabled": False,
                }
            ],
        )

        assert nt.deliver_single_channel(notification.pk, Channel.SMS) == "skipped_pref"
        assert not NotificationDelivery.objects.filter(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
        ).exists()
        assert (
            NotificationDelivery.objects.filter(
                notification=notification,
                channel=Channel.SMS,
                status=NotificationDelivery.Status.SKIPPED_PREF,
            ).count()
            == 1
        )


# --------------------------------------------------------------------------- #
# In-app WS group name must be schema-prefixed (cross-tenant isolation)
# --------------------------------------------------------------------------- #
@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_in_app_group_send_is_schema_prefixed(tenant_a, monkeypatch, django_capture_on_commit_callbacks):
    """The producer uses one tenant + role-principal group, never a bridge user."""
    from apps.notifications.services import dispatch

    captured: list[str] = []

    def fake_group_send(group, message):
        captured.append(group)

    # _deliver_in_app imports group_send from this module at call time, so
    # patching the source-module attribute is what takes effect.
    monkeypatch.setattr("infrastructure.websocket.channel_layer.group_send", fake_group_send)

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name), django_capture_on_commit_callbacks(execute=True):
        dispatch(
            event_type="attendance.absent",
            recipient_id=user.pk,
            context={"lesson_id": 1},
            channels=["in_app"],  # in-app only
            dedupe_key=f"grp:{user.pk}",
        )

    assert captured, "in-app delivery must call group_send"
    group = captured[0]
    assert group == (
        f"{tenant_a.schema_name}.n.{user.notification_principal_kind}.{user.notification_principal_id}"
    )
    assert group.startswith(f"{tenant_a.schema_name}.")
    # Neither the pre-tenant nor bridge-user group may carry private events.
    assert group != f"user.{user.pk}"
    assert group != f"{tenant_a.schema_name}.user.{user.pk}"


# --------------------------------------------------------------------------- #
# Large cohort fan-out offloads to chunked Celery but still reaches everyone
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", [3, 30])
def test_dispatch_many_reaches_all_recipients_inline_and_offloaded(tenant_a, monkeypatch, n):
    """R2-11: a small fan-out dispatches inline; a large one (> _FANOUT_INLINE_MAX)
    offloads to chunked Celery — but every recipient must still be dispatched exactly
    once (eager Celery runs the chunk synchronously). Same delivered set either way."""
    from apps.notifications import receivers

    calls: list[int] = []
    monkeypatch.setattr(
        "apps.notifications.services.dispatch",
        lambda **kwargs: calls.append(kwargs["recipient_id"]),
    )
    with schema_context(tenant_a.schema_name):
        receivers._dispatch_many(
            user_ids=list(range(1, n + 1)),
            event_type="attendance.absent",
            context={"lesson_id": 1},
            dedupe_prefix="t",
        )
    assert sorted(calls) == list(range(1, n + 1))  # all reached, no drops/dupes


# --------------------------------------------------------------------------- #
# Repeat lesson reschedules must each notify (dedupe key includes the move)
# --------------------------------------------------------------------------- #
def test_lesson_reschedule_dedupe_key_varies_per_move(tenant_a, monkeypatch):
    """R2-03: a lesson rescheduled twice (move A->B, then B->C) must notify BOTH
    times. Keying the dedupe on lesson_id alone collapsed every reschedule after the
    first into one suppressed notification — the highest-impact (latest) move went
    silent. The key must include the per-move discriminator (old_start)."""
    from apps.notifications import receivers
    from apps.schedule.services import lesson_rescheduled

    keys: list[str] = []

    def _capture(*, recipients, event_type, context, dedupe_prefix, **kw):
        keys.append(dedupe_prefix)

    monkeypatch.setattr(receivers, "_dispatch_many", _capture)

    with schema_context(tenant_a.schema_name):
        # Move A->B, back B->A, then A->B AGAIN: old_start repeats (A, B, A) but moved_at
        # (the lesson's updated_at) is monotonic, so all three keys are distinct — the
        # 3rd move must still notify (old_start alone would collide keys[0]==keys[2]).
        lesson_rescheduled.send(
            sender=None,
            lesson_id=42,
            old_start="2026-01-05T10:00:00+00:00",
            moved_at="2026-01-01T08:00:00.000001+00:00",
            schema_name=tenant_a.schema_name,
        )
        lesson_rescheduled.send(
            sender=None,
            lesson_id=42,
            old_start="2026-01-12T10:00:00+00:00",
            moved_at="2026-01-01T08:00:00.000002+00:00",
            schema_name=tenant_a.schema_name,
        )
        lesson_rescheduled.send(
            sender=None,
            lesson_id=42,
            old_start="2026-01-05T10:00:00+00:00",  # same old_start as #1
            moved_at="2026-01-01T08:00:00.000003+00:00",
            schema_name=tenant_a.schema_name,
        )

    assert len(keys) == 3, "all three moves must dispatch"
    assert len(set(keys)) == 3, "each move must get a distinct dedupe key (monotonic moved_at)"
    assert all(k.startswith("schedule.lesson_rescheduled:42:") for k in keys)


# --------------------------------------------------------------------------- #
# Realtime push is best-effort: a channel-layer (Redis) outage must not raise
# --------------------------------------------------------------------------- #
def test_group_send_swallows_channel_layer_failure(monkeypatch):
    """A realtime broadcast runs inside transaction.on_commit hooks; if it raised
    (Redis down) it would 500 an already-committed request AND abort the remaining
    on_commit callbacks — e.g. dropping the guardian notifications of every later
    absent student in a mark-attendance batch. group_send must swallow + log."""
    from infrastructure.websocket import channel_layer

    class _BoomLayer:
        async def group_send(self, group, message):
            raise ConnectionError("redis down")

    monkeypatch.setattr(channel_layer, "get_channel_layer", lambda: _BoomLayer())
    # Must NOT raise despite the layer erroring.
    channel_layer.group_send("tenant.cohort.1", {"type": "attendance.update"})


def test_group_send_no_layer_configured_is_noop(monkeypatch):
    """No channel layer (e.g. a management command context) is a silent no-op,
    never an AttributeError on None.group_send."""
    from infrastructure.websocket import channel_layer

    monkeypatch.setattr(channel_layer, "get_channel_layer", lambda: None)
    channel_layer.group_send("tenant.cohort.1", {"type": "attendance.update"})


# --------------------------------------------------------------------------- #
# Quiet-hours deferral is idempotent under dispatch_notification redelivery
# --------------------------------------------------------------------------- #
@time_machine.travel("2026-06-16 23:30:00 +05:00", tick=False)
def test_quiet_hours_redelivery_does_not_double_defer(
    tenant_a, monkeypatch, django_capture_on_commit_callbacks
):
    """A Celery redelivery of ``dispatch_notification`` for a quiet-hours channel
    that already has a SKIPPED_QUIET_HOURS marker must NOT record a second skip.
    The due reconciler must lease the marker exactly once per retry window."""
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, Notification, NotificationDelivery
    from apps.notifications.services import dispatch

    _set_quiet_hours(tenant_a, start=time(22, 0), end=time(7, 0))

    schedule_calls: list[tuple[tuple, dict]] = []

    def fake_delay(*args, **kwargs):
        schedule_calls.append((args, kwargs))

    monkeypatch.setattr(nt.deliver_single_channel, "delay", fake_delay)

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        with django_capture_on_commit_callbacks(execute=True):
            notif = dispatch(
                event_type="payments.payment_completed",
                recipient_id=user.pk,
                context={"amount": "1"},
                dedupe_key=f"qh:{user.pk}",
            )
        # First dispatch writes one durable marker and no broker ETA.
        sms_skips = NotificationDelivery.objects.filter(
            notification=notif, channel=Channel.SMS, status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS
        ).count()
        assert sms_skips == 1
        assert schedule_calls == []

        # Simulate Celery redelivering the SAME dispatch task.
        nt.dispatch_notification(notif.pk)

        # No second skip marker or immediate broker work.
        assert (
            NotificationDelivery.objects.filter(
                notification=notif,
                channel=Channel.SMS,
                status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
            ).count()
            == 1
        )
        assert schedule_calls == []
        # sanity: still a single notification row
        assert Notification.objects.filter(pk=notif.pk).count() == 1

    with (
        time_machine.travel("2026-06-17 07:00:00 +05:00", tick=False),
        schema_context(tenant_a.schema_name),
        django_capture_on_commit_callbacks(execute=True),
    ):
        assert nt.reconcile_deferred_notification_deliveries_for_schema() >= 1
        # The first sweep wrote a five-minute lease before publishing. A
        # concurrent/repeated sweep cannot enqueue the same marker again.
        assert nt.reconcile_deferred_notification_deliveries_for_schema() == 0
    sms_schedules = [call for call in schedule_calls if call[0][1] == Channel.SMS]
    assert len(sms_schedules) == 1


@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_deliver_single_channel_second_run_is_noop(tenant_a, sms_outbox, django_capture_on_commit_callbacks):
    """``deliver_single_channel`` must send at most once: a redelivery (or two
    skip markers producing two scheduled tasks) at window end sends ONE SMS."""
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.notifications.services import dispatch
    from celery_tasks.notification_tasks import deliver_single_channel

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        # Create a notification with a standing SKIPPED_QUIET_HOURS SMS marker,
        # as the quiet-hours branch would have left.
        with django_capture_on_commit_callbacks(execute=False):
            notif = dispatch(
                event_type="payments.payment_completed",
                recipient_id=user.pk,
                context={"amount": "1"},
                dedupe_key=f"dsc:{user.pk}",
            )
        NotificationDelivery.objects.create(
            notification=notif,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
            provider_response={"deferred_to": "2026-06-16T07:00:00+05:00"},
        )
        sms_outbox.clear()

        # First window-end run: delivers (deferred_to in the past).
        r1 = deliver_single_channel(notif.pk, Channel.SMS, deferred_to="2026-06-16T07:00:00+05:00")
        # Second run (redelivery): must no-op.
        r2 = deliver_single_channel(notif.pk, Channel.SMS, deferred_to="2026-06-16T07:00:00+05:00")

    assert r1 == "sent"
    assert r2 == "already_delivered"
    assert len(sms_outbox) == 1  # exactly one SMS, never two


@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_provider_exception_does_not_abort_later_channels_and_is_retried(
    tenant_a,
    sms_outbox,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, EventType, Notification, NotificationDelivery
    from infrastructure.email import email_client

    user = _user_with_phone(tenant_a)
    retry_calls: list[dict] = []
    monkeypatch.setattr(nt.deliver_single_channel, "apply_async", lambda **kwargs: retry_calls.append(kwargs))

    def unavailable_email(**kwargs):
        raise ConnectionError("provider detail that must not be persisted")

    monkeypatch.setattr(email_client, "send_email", unavailable_email)
    with schema_context(tenant_a.schema_name):
        notification = Notification.objects.create(
            user=user,
            event_type=EventType.FINANCE_INVOICE_ISSUED,
            title="Invoice",
            body="An invoice is ready.",
        )
        with django_capture_on_commit_callbacks(execute=True):
            result = nt.dispatch_notification(
                notification.pk,
                channels=[Channel.EMAIL, Channel.SMS],
            )

        failed_email = NotificationDelivery.objects.get(
            notification=notification,
            channel=Channel.EMAIL,
            status=NotificationDelivery.Status.FAILED,
        )
        assert failed_email.provider_response == {
            "error": "ConnectionError",
            "retryable": True,
        }
        assert NotificationDelivery.objects.filter(
            notification=notification,
            channel=Channel.SMS,
            status=NotificationDelivery.Status.SENT,
        ).exists()
        assert result["results"][Channel.EMAIL] == "failed_retrying"
        assert result["results"][Channel.SMS] == "sent"
        assert retry_calls[0]["kwargs"]["attempt"] == 1

        # The retry sends only the unfinished channel; the successful SMS stays
        # terminal and cannot be duplicated by the fan-out retry path.
        monkeypatch.setattr(email_client, "send_email", lambda **kwargs: None)
        assert nt.deliver_single_channel(notification.pk, Channel.EMAIL, attempt=1) == "sent"
        assert NotificationDelivery.objects.filter(
            notification=notification,
            channel=Channel.EMAIL,
            status=NotificationDelivery.Status.SENT,
        ).exists()

    assert len(sms_outbox) == 1


@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_push_retry_targets_only_the_failed_device(
    tenant_a,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, EventType, Notification, NotificationDelivery
    from apps.notifications.tests.helpers import session_principal_kwargs
    from apps.users.models import Device
    from core.session_auth import create_session
    from infrastructure.push import fcm_client

    user = _user_with_phone(tenant_a)
    retry_calls: list[dict] = []
    monkeypatch.setattr(nt.deliver_single_channel, "apply_async", lambda **kwargs: retry_calls.append(kwargs))

    class PartlyUnavailablePush:
        def __init__(self):
            self.calls: list[str] = []

        def send(self, *, token, **kwargs):
            self.calls.append(token)
            if token == "fails-once":
                raise ConnectionError("temporary push outage")
            return {"success": True, "message_id": f"sent-{token}"}

    first_client = PartlyUnavailablePush()
    monkeypatch.setattr(fcm_client, "get_push_client", lambda: first_client)

    with schema_context(tenant_a.schema_name):
        Device.objects.create(user=user, device_id="device-1", platform="android", push_token="fails-once")
        Device.objects.create(user=user, device_id="device-2", platform="ios", push_token="already-sent")
        create_session(user, device_id="device-1", **session_principal_kwargs(user))
        create_session(user, device_id="device-2", **session_principal_kwargs(user))
        notification = Notification.objects.create(
            user=user,
            event_type=EventType.ASSIGNMENTS_CREATED,
            title="Assignment",
            body="A new assignment is ready.",
        )
        with django_capture_on_commit_callbacks(execute=True):
            result = nt.dispatch_notification(notification.pk, channels=[Channel.PUSH])

        assert result["results"][Channel.PUSH] == "failed_retrying"
        assert retry_calls[0]["kwargs"]["attempt"] == 1
        assert NotificationDelivery.objects.filter(
            notification=notification,
            channel=Channel.PUSH,
            status=NotificationDelivery.Status.SENT,
            provider_response__device_id="device-2",
        ).exists()

        retried_tokens: list[str] = []

        class RecoveredPush:
            def send(self, *, token, **kwargs):
                retried_tokens.append(token)
                return {"success": True, "message_id": f"retry-{token}"}

        monkeypatch.setattr(fcm_client, "get_push_client", lambda: RecoveredPush())
        assert nt.deliver_single_channel(notification.pk, Channel.PUSH, attempt=1) == "sent"
        assert retried_tokens == ["fails-once"]


@override_settings(PUSH_NOTIFICATIONS_ENABLED=True)
@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_push_targets_only_devices_with_active_unexpired_sessions(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, EventType, Notification
    from apps.notifications.tests.helpers import session_principal_kwargs
    from apps.users.models import Device, Session
    from core.session_auth import create_session
    from infrastructure.push import fcm_client

    user = _user_with_phone(tenant_a)
    sent_tokens: list[str] = []
    sent_payloads: list[dict[str, str]] = []

    class RecordingPush:
        def send(self, *, token, **kwargs):
            sent_tokens.append(token)
            sent_payloads.append(kwargs["data"])
            return {"success": True, "message_id": f"sent-{token}"}

    monkeypatch.setattr(fcm_client, "get_push_client", lambda: RecordingPush())

    with schema_context(tenant_a.schema_name):
        Device.objects.create(
            user=user,
            device_id="live-device",
            platform="ios",
            push_token="live-token",
        )
        Device.objects.create(
            user=user,
            device_id="expired-device",
            platform="android",
            push_token="expired-token",
        )
        Device.objects.create(
            user=user,
            device_id="revoked-device",
            platform="android",
            push_token="revoked-token",
        )
        create_session(user, device_id="live-device", **session_principal_kwargs(user))
        expired = create_session(user, device_id="expired-device", **session_principal_kwargs(user))
        revoked = create_session(user, device_id="revoked-device", **session_principal_kwargs(user))
        Session.objects.filter(pk=expired.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        Session.objects.filter(pk=revoked.pk).update(revoked_at=timezone.now())
        notification = Notification.objects.create(
            user=user,
            event_type=EventType.ASSIGNMENTS_CREATED,
            title="Assignment",
            body="A new assignment is ready.",
        )

        result = nt.dispatch_notification(notification.pk, channels=[Channel.PUSH])

    assert result["results"][Channel.PUSH] == "sent"
    assert sent_tokens == ["live-token"]
    assert sent_payloads[0]["tenant_slug"] == tenant_a.schema_name


@override_settings(PUSH_NOTIFICATIONS_ENABLED=True)
@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_push_with_only_expired_session_records_no_devices(tenant_a, monkeypatch):
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, EventType, Notification, NotificationDelivery
    from apps.notifications.tests.helpers import session_principal_kwargs
    from apps.users.models import Device, Session
    from core.session_auth import create_session
    from infrastructure.push import fcm_client

    user = _user_with_phone(tenant_a)

    class UnexpectedPush:
        def send(self, **kwargs):
            pytest.fail("an expired app session received a private push")

    monkeypatch.setattr(fcm_client, "get_push_client", lambda: UnexpectedPush())

    with schema_context(tenant_a.schema_name):
        Device.objects.create(
            user=user,
            device_id="expired-only",
            platform="ios",
            push_token="must-not-send",
        )
        session = create_session(user, device_id="expired-only", **session_principal_kwargs(user))
        Session.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        notification = Notification.objects.create(
            user=user,
            event_type=EventType.ASSIGNMENTS_CREATED,
            title="Assignment",
            body="A new assignment is ready.",
        )

        result = nt.dispatch_notification(notification.pk, channels=[Channel.PUSH])
        delivery = NotificationDelivery.objects.get(
            notification=notification,
            channel=Channel.PUSH,
        )

    assert result["results"][Channel.PUSH] == "failed_no_devices"
    assert delivery.provider_response == {"error": "no_devices"}
