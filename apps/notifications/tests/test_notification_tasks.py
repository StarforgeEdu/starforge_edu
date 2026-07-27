"""Regression tests for the notification Celery fan-out (celery_tasks/notification_tasks).

Covers the verified bugs in the messaging cluster:
- in-app/WS group name is SCHEMA-prefixed (cross-tenant leak on shared Redis).
- quiet-hours deferral is idempotent under Celery at-least-once redelivery of
  ``dispatch_notification`` (no double SMS/push), and ``deliver_single_channel``
  no-ops on a second deferred run.
"""

from __future__ import annotations

from datetime import time

import pytest
import time_machine
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db


def test_push_device_history_has_targeted_expression_index():
    from apps.notifications.models import NotificationDelivery

    indexes = {index.name: index for index in NotificationDelivery._meta.indexes}
    index = indexes["notif_push_device_created_idx"]
    assert index.include == ("notification", "status")
    assert len(index.expressions) == 2


def _user_with_phone(tenant):
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        return UserFactory(phone="+998901112233", email="payer@example.com")


def _set_quiet_hours(tenant, *, start, end):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.quiet_hours_start = start
        cs.quiet_hours_end = end
        cs.save(update_fields=["quiet_hours_start", "quiet_hours_end"])


# --------------------------------------------------------------------------- #
# In-app WS group name must be schema-prefixed (cross-tenant isolation)
# --------------------------------------------------------------------------- #
@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_in_app_group_send_is_schema_prefixed(tenant_a, monkeypatch, django_capture_on_commit_callbacks):
    """The producer group name MUST be ``{schema}.user.{id}`` — an unscoped
    ``user.{id}`` collides across tenants on the shared Redis channel layer
    (tenant A user 5 receives tenant B user 5's notifications)."""
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
    assert group == f"{tenant_a.schema_name}.user.{user.pk}"
    assert group.startswith(f"{tenant_a.schema_name}.")
    # The pre-fix unscoped name must never be produced.
    assert group != f"user.{user.pk}"


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
        lambda *, event_type, recipient_id, context, dedupe_key=None: calls.append(recipient_id),
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

    def _capture(*, user_ids, event_type, context, dedupe_prefix, **kw):
        keys.append(dedupe_prefix)

    monkeypatch.setattr(receivers, "_dispatch_many", _capture)

    with schema_context(tenant_a.schema_name):
        # Move A->B, back B->A, then A->B AGAIN: old_start repeats (A, B, A) but moved_at
        # (the lesson's updated_at) is monotonic, so all three keys are distinct — the
        # 3rd move must still notify (old_start alone would collide keys[0]==keys[2]).
        lesson_rescheduled.send(
            sender=None, lesson_id=42, old_start="2026-01-05T10:00:00+00:00",
            moved_at="2026-01-01T08:00:00.000001+00:00", schema_name=tenant_a.schema_name,
        )
        lesson_rescheduled.send(
            sender=None, lesson_id=42, old_start="2026-01-12T10:00:00+00:00",
            moved_at="2026-01-01T08:00:00.000002+00:00", schema_name=tenant_a.schema_name,
        )
        lesson_rescheduled.send(
            sender=None, lesson_id=42, old_start="2026-01-05T10:00:00+00:00",  # same old_start as #1
            moved_at="2026-01-01T08:00:00.000003+00:00", schema_name=tenant_a.schema_name,
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
    that already has a SKIPPED_QUIET_HOURS marker must NOT record a second skip
    nor schedule a second deferred delivery (which would double-send paid SMS)."""
    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, Notification, NotificationDelivery
    from apps.notifications.services import dispatch

    _set_quiet_hours(tenant_a, start=time(22, 0), end=time(7, 0))

    schedule_calls: list[dict] = []

    def fake_apply_async(*args, **kwargs):
        schedule_calls.append(kwargs)
        return None

    monkeypatch.setattr(nt.deliver_single_channel, "apply_async", fake_apply_async)

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        with django_capture_on_commit_callbacks(execute=True):
            notif = dispatch(
                event_type="payments.payment_completed",
                recipient_id=user.pk,
                context={"amount": "1"},
                dedupe_key=f"qh:{user.pk}",
            )
        # First dispatch already ran the fan-out once (one SMS deferral scheduled).
        sms_skips = NotificationDelivery.objects.filter(
            notification=notif, channel=Channel.SMS, status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS
        ).count()
        assert sms_skips == 1
        first_schedule_count = len(schedule_calls)
        assert first_schedule_count >= 1

        # Simulate Celery redelivering the SAME dispatch task.
        nt.dispatch_notification(notif.pk)

        # No SECOND skip marker, no SECOND scheduled deferral for SMS.
        assert (
            NotificationDelivery.objects.filter(
                notification=notif,
                channel=Channel.SMS,
                status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
            ).count()
            == 1
        )
        # The redelivery must not have scheduled additional SMS deferrals.
        sms_schedules = [c for c in schedule_calls if (c.get("kwargs") or {}).get("channel") == Channel.SMS]
        assert len(sms_schedules) == 1
        # sanity: still a single notification row
        assert Notification.objects.filter(pk=notif.pk).count() == 1


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
    from apps.users.models import Device
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
