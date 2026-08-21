"""D3-F-8 — notification preference matrix + quiet hours + dedupe under double-fire.

Adversarial coverage of the fan-out (D3-C-5/8):

  - A user who DISABLED SMS for ``payments.payment_completed`` gets the in-app
    delivery but NO SMS — MockEskiz.outbox stays empty, and the SMS channel is
    recorded as ``skipped_pref`` (not silently dropped).
  - During quiet hours, the SMS channel is deferred in a durable database
    marker with ``deferred_to`` equal to the quiet-window end. The periodic
    reconciler enqueues it only after that time; in-app still delivers
    immediately and Redis never holds an hours-long ETA reservation.
  - Double-fire of the same event (same ``dedupe_key``) collapses to ONE
    Notification and ONE SMS send (idempotent dispatch + idempotent fan-out).

Mechanics:
- ``dispatch()`` queues the fan-out via ``transaction.on_commit``, so tests wrap
  it in ``django_capture_on_commit_callbacks(execute=True)`` to actually run the
  task (the repo convention — see apps/attendance/tests).
- Celery is eager in tests, so the quiet-hours test invokes the durable
  reconciler under a second frozen clock at the window boundary.
- Times use ``time_machine`` with explicit Asia/Tashkent offsets (TESTING.md §5).
"""

from __future__ import annotations

from datetime import time

import pytest
import time_machine
from django_tenants.utils import schema_context

pytestmark = pytest.mark.django_db

EVENT = "payments.payment_completed"  # SMS defaults ON for payments.* (DEFAULT_MATRIX)


def _user_with_phone(tenant):
    from apps.notifications.tests.helpers import ensure_notification_principal
    from apps.users.tests.factories import UserFactory

    with schema_context(tenant.schema_name):
        user = UserFactory(phone="+998901112233", email="payer@example.com")
        return ensure_notification_principal(user)


def _deliveries(tenant, notification_id):
    from apps.notifications.models import NotificationDelivery

    with schema_context(tenant.schema_name):
        return list(NotificationDelivery.objects.filter(notification_id=notification_id))


def _set_quiet_hours(tenant, *, start, end):
    from apps.org.models import CenterSettings

    with schema_context(tenant.schema_name):
        cs = CenterSettings.load()
        cs.quiet_hours_start = start
        cs.quiet_hours_end = end
        cs.save(update_fields=["quiet_hours_start", "quiet_hours_end"])


# --------------------------------------------------------------------------- #
# Preference: disabled SMS -> in-app only, MockEskiz never called
# --------------------------------------------------------------------------- #
# Fire outside quiet hours (12:00 Asia/Tashkent) so the SMS path is reached.
@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_disabled_sms_gets_in_app_not_sms(tenant_a, sms_outbox, django_capture_on_commit_callbacks):
    from apps.notifications.models import Channel, NotificationDelivery, NotificationPreference
    from apps.notifications.services import dispatch

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        NotificationPreference.objects.create(user=user, event_type=EVENT, channel=Channel.SMS, enabled=False)
        with django_capture_on_commit_callbacks(execute=True):
            notif = dispatch(
                event_type=EVENT,
                recipient_id=user.pk,
                context={"amount": "150000"},
                dedupe_key=f"{EVENT}:{user.pk}:pref-off",
            )

    assert notif is not None
    assert sms_outbox == []  # MockEskiz never called for the disabled channel

    rows = {d.channel: d.status for d in _deliveries(tenant_a, notif.pk)}
    assert rows.get(Channel.SMS) == NotificationDelivery.Status.SKIPPED_PREF
    assert rows.get(Channel.IN_APP) == NotificationDelivery.Status.SENT


@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_default_on_sms_is_sent_when_not_disabled(tenant_a, sms_outbox, django_capture_on_commit_callbacks):
    """Control: with no opt-out, payments.* SMS IS sent (one outbox entry)."""
    from apps.notifications.services import dispatch

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name), django_capture_on_commit_callbacks(execute=True):
        dispatch(
            event_type=EVENT,
            recipient_id=user.pk,
            context={"amount": "150000"},
            dedupe_key=f"{EVENT}:{user.pk}:on",
        )
    assert len(sms_outbox) == 1
    assert sms_outbox[0]["phone"] == "+998901112233"


# --------------------------------------------------------------------------- #
# Quiet hours: SMS deferred with eta == window end; in-app immediate
# --------------------------------------------------------------------------- #
@time_machine.travel("2026-06-16 23:30:00 +05:00", tick=False)
def test_quiet_hours_sms_uses_durable_outbox_until_window_end(
    tenant_a, sms_outbox, monkeypatch, django_capture_on_commit_callbacks
):
    from django.utils import timezone

    import celery_tasks.notification_tasks as nt
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.notifications.services import dispatch, quiet_hours_eta

    _set_quiet_hours(tenant_a, start=time(22, 0), end=time(7, 0))

    monkeypatch.setattr(
        nt.deliver_single_channel,
        "apply_async",
        lambda *args, **kwargs: pytest.fail("quiet-hours work must not use a broker ETA"),
    )

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        with django_capture_on_commit_callbacks(execute=True):
            notif = dispatch(
                event_type=EVENT,
                recipient_id=user.pk,
                context={"amount": "150000"},
                dedupe_key=f"{EVENT}:{user.pk}:quiet",
            )
        expected_eta = quiet_hours_eta(at=timezone.now(), end=time(7, 0))

    rows = {d.channel: d for d in _deliveries(tenant_a, notif.pk)}
    # In-app delivered immediately even during quiet hours.
    assert rows[Channel.IN_APP].status == NotificationDelivery.Status.SENT
    # SMS deferred, NOT sent inline.
    assert sms_outbox == []
    sms = rows[Channel.SMS]
    assert sms.status == NotificationDelivery.Status.SKIPPED_QUIET_HOURS
    assert sms.provider_response.get("deferred_to") == expected_eta.isoformat()
    assert expected_eta.hour == 7
    assert expected_eta.minute == 0

    queued: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        nt.deliver_single_channel,
        "delay",
        lambda *args, **kwargs: queued.append((args, kwargs)),
    )
    with (
        time_machine.travel("2026-06-17 07:00:00 +05:00", tick=False),
        schema_context(tenant_a.schema_name),
        django_capture_on_commit_callbacks(execute=True),
    ):
        assert nt.reconcile_deferred_notification_deliveries_for_schema() >= 1
    sms_jobs = [job for job in queued if job[0][1] == Channel.SMS]
    assert sms_jobs == [
        (
            (notif.pk, Channel.SMS),
            {"deferred_to": expected_eta.isoformat(), "_schema_name": tenant_a.schema_name},
        )
    ]


def test_quiet_hours_helpers_wraparound():
    """Pure-function guard for the wrap-around (22:00-07:00) window."""
    from datetime import datetime

    from django.utils import timezone

    from apps.notifications.services import in_quiet_hours

    start, end = time(22, 0), time(7, 0)

    def at(hour, minute=0):
        return timezone.make_aware(datetime(2026, 6, 16, hour, minute))

    assert in_quiet_hours(at=at(23, 30), start=start, end=end) is True
    assert in_quiet_hours(at=at(2, 0), start=start, end=end) is True
    assert in_quiet_hours(at=at(6, 59), start=start, end=end) is True
    assert in_quiet_hours(at=at(7, 0), start=start, end=end) is False  # [start,end)
    assert in_quiet_hours(at=at(12, 0), start=start, end=end) is False


# --------------------------------------------------------------------------- #
# Dedupe under signal double-fire: one Notification, one SMS
# --------------------------------------------------------------------------- #
@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_double_fire_dedupes_one_notification_one_sms(
    tenant_a, sms_outbox, django_capture_on_commit_callbacks
):
    from apps.notifications.models import Notification
    from apps.notifications.services import dispatch

    user = _user_with_phone(tenant_a)
    key = f"{EVENT}:{user.pk}:dedupe"
    with schema_context(tenant_a.schema_name):
        with django_capture_on_commit_callbacks(execute=True):
            first = dispatch(event_type=EVENT, recipient_id=user.pk, context={"amount": "1"}, dedupe_key=key)
        with django_capture_on_commit_callbacks(execute=True):
            second = dispatch(event_type=EVENT, recipient_id=user.pk, context={"amount": "1"}, dedupe_key=key)
        assert first.pk == second.pk
        assert Notification.objects.filter(dedupe_key=key).count() == 1

    # The second dispatch is a no-op (does not re-queue fan-out) -> one SMS only.
    assert len(sms_outbox) == 1


@time_machine.travel("2026-06-16 12:00:00 +05:00", tick=False)
def test_fan_out_idempotent_on_task_rerun(tenant_a, sms_outbox, django_capture_on_commit_callbacks):
    """Re-running the fan-out task for the same notification does not double-send
    (the (notification, channel) delivery guard)."""
    from apps.notifications.services import dispatch
    from celery_tasks.notification_tasks import dispatch_notification

    user = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name):
        with django_capture_on_commit_callbacks(execute=True):
            notif = dispatch(
                event_type=EVENT,
                recipient_id=user.pk,
                context={"amount": "1"},
                dedupe_key=f"{EVENT}:{user.pk}:rerun",
            )
        # dispatch already ran the task once. Run it AGAIN explicitly.
        dispatch_notification(notif.pk)

    assert len(sms_outbox) == 1  # still exactly one SMS, not two


# --------------------------------------------------------------------------- #
# Cross-tenant: a notification fired in A is invisible in B
# --------------------------------------------------------------------------- #
def test_dispatch_isolated_per_schema(tenant_a, tenant_b, django_capture_on_commit_callbacks):
    from apps.notifications.models import Notification
    from apps.notifications.services import dispatch

    user_a = _user_with_phone(tenant_a)
    with schema_context(tenant_a.schema_name), django_capture_on_commit_callbacks(execute=True):
        dispatch(event_type=EVENT, recipient_id=user_a.pk, context={}, dedupe_key="iso-a")

    with schema_context(tenant_a.schema_name):
        assert Notification.objects.filter(dedupe_key="iso-a").count() == 1
    with schema_context(tenant_b.schema_name):
        assert Notification.objects.filter(dedupe_key="iso-a").count() == 0
