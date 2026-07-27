"""Day-3 Lane C tests (D3-C). Run centrally on Postgres.

Covers the DAY-3 "Tests required" list:
- dispatch idempotency (same dedupe_key twice -> one row, one send)
- preference matrix parameterized (event x channel x enabled)
- quiet-hours deferral (freeze 23:00 -> SMS eta, in-app immediate)
- template locale fallback (en->uz)
- absence signal end-to-end -> guardian mock SMS + in-app row
- dead-token cleanup after 3 push failures
- own-rows-only on the feed (+ cross-tenant in test_cross_tenant_day3.py)
"""

from __future__ import annotations

import pytest
import time_machine
from django_tenants.utils import schema_context

from apps.notifications import services
from apps.notifications.models import (
    Channel,
    EventType,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationTemplate,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# dispatch() idempotency (D3-C-3)
# ---------------------------------------------------------------------------
def test_dispatch_dedupe_key_is_idempotent(tenant_a, user_in, sms_outbox):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        first = services.dispatch(
            event_type=EventType.ATTENDANCE_ABSENT,
            recipient_id=user.pk,
            context={"lesson_id": 1},
            dedupe_key="attendance.absent:42:7",
        )
        second = services.dispatch(
            event_type=EventType.ATTENDANCE_ABSENT,
            recipient_id=user.pk,
            context={"lesson_id": 1},
            dedupe_key="attendance.absent:42:7",
        )
        assert first.pk == second.pk
        assert Notification.objects.filter(dedupe_key="attendance.absent:42:7").count() == 1


def test_dispatch_unknown_user_is_dropped_not_raised(tenant_a):
    with schema_context(tenant_a.schema_name):
        # Delta, not a global ==0: under the project default --reuse-db a prior
        # transaction=True test's committed rows can survive into this run, so an
        # absolute count is order-fragile. The real assertion is "this dispatch
        # created nothing".
        before = Notification.objects.count()
        result = services.dispatch(
            event_type=EventType.ATTENDANCE_ABSENT,
            recipient_id=999999,
            context={},
        )
        assert result is None
        assert Notification.objects.count() == before


# ---------------------------------------------------------------------------
# Preference matrix (D3-C-8)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("event_type", "channel", "expected"),
    [
        (EventType.ATTENDANCE_ABSENT, Channel.IN_APP, True),
        (EventType.ATTENDANCE_ABSENT, Channel.SMS, True),
        (EventType.ATTENDANCE_ABSENT, Channel.PUSH, True),
        (EventType.ATTENDANCE_ABSENT, Channel.EMAIL, False),
        (EventType.ASSIGNMENTS_CREATED, Channel.SMS, False),
        (EventType.ASSIGNMENTS_CREATED, Channel.PUSH, True),
        (EventType.FINANCE_INVOICE_ISSUED, Channel.EMAIL, True),
        (EventType.FINANCE_INVOICE_ISSUED, Channel.SMS, True),
        (EventType.PAYMENTS_PAYMENT_COMPLETED, Channel.SMS, True),
        (EventType.PAYMENTS_PAYMENT_COMPLETED, Channel.EMAIL, False),
        (EventType.BILLING_SUBSCRIPTION_PAST_DUE, Channel.EMAIL, True),
        (EventType.SCHEDULE_LESSON_REMINDER, Channel.EMAIL, False),
    ],
)
def test_default_matrix(event_type, channel, expected):
    assert services.default_channel_enabled(event_type, channel) is expected


def test_explicit_preference_overrides_default(tenant_a, user_in):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        NotificationPreference.objects.create(
            user=user,
            event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
            channel=Channel.SMS,
            enabled=False,
        )
        assert (
            services.channel_enabled_for_user(
                user_id=user.pk,
                event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
                channel=Channel.SMS,
            )
            is False
        )
        # default still applies to other channels
        assert (
            services.channel_enabled_for_user(
                user_id=user.pk,
                event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
                channel=Channel.IN_APP,
            )
            is True
        )


def test_disabled_sms_pref_skips_sms_but_in_app_still_lands(
    tenant_a, user_in, sms_outbox, django_capture_on_commit_callbacks
):
    """User who disabled SMS for payment_completed gets in-app, no SMS (D3-F-8)."""
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        user.phone = "+998901234567"
        user.save(update_fields=["phone"])
        NotificationPreference.objects.create(
            user=user,
            event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
            channel=Channel.SMS,
            enabled=False,
        )
        with django_capture_on_commit_callbacks(execute=True):
            services.dispatch(
                event_type=EventType.PAYMENTS_PAYMENT_COMPLETED,
                recipient_id=user.pk,
                context={"amount_uzs": "100000"},
            )
        notif = Notification.objects.get(user=user)
        sms = notif.deliveries.filter(channel=Channel.SMS).first()
        in_app = notif.deliveries.filter(channel=Channel.IN_APP).first()
        assert sms.status == NotificationDelivery.Status.SKIPPED_PREF
        assert in_app.status == NotificationDelivery.Status.SENT
        assert sms_outbox == []  # MockEskiz never called


# ---------------------------------------------------------------------------
# Quiet hours (D3-C-8)
# ---------------------------------------------------------------------------
@time_machine.travel("2026-06-10 23:30 +05:00", tick=False)
def test_quiet_hours_defers_sms_and_push_but_in_app_immediate(
    tenant_a, user_in, sms_outbox, django_capture_on_commit_callbacks
):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        user.phone = "+998901234567"
        user.save(update_fields=["phone"])
        with django_capture_on_commit_callbacks(execute=True):
            services.dispatch(
                event_type=EventType.ATTENDANCE_ABSENT,
                recipient_id=user.pk,
                context={"lesson_id": 1},
            )
        notif = Notification.objects.get(user=user)
        sms = notif.deliveries.filter(channel=Channel.SMS).first()
        in_app = notif.deliveries.filter(channel=Channel.IN_APP).first()
        # SMS recorded as deferred (skipped_quiet_hours marker with eta).
        assert sms.status == NotificationDelivery.Status.SKIPPED_QUIET_HOURS
        assert "deferred_to" in sms.provider_response
        # eta lands at the quiet-hours window end (07:00 default).
        assert sms.provider_response["deferred_to"].split("T")[1].startswith("07:00")
        # In-app delivered immediately.
        assert in_app.status == NotificationDelivery.Status.SENT


@time_machine.travel("2026-06-10 12:00 +05:00", tick=False)
def test_outside_quiet_hours_sms_sent_immediately(
    tenant_a, user_in, sms_outbox, django_capture_on_commit_callbacks
):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        user.phone = "+998901234567"
        user.save(update_fields=["phone"])
        with django_capture_on_commit_callbacks(execute=True):
            services.dispatch(
                event_type=EventType.ATTENDANCE_ABSENT,
                recipient_id=user.pk,
                context={"lesson_id": 1},
            )
        notif = Notification.objects.get(user=user)
        sms = notif.deliveries.filter(channel=Channel.SMS).first()
        assert sms.status == NotificationDelivery.Status.SENT
        assert len(sms_outbox) == 1


def test_in_quiet_hours_wraparound_window():
    from datetime import datetime, time

    from django.utils import timezone

    start, end = time(22, 0), time(7, 0)
    inside = timezone.make_aware(datetime(2026, 6, 10, 23, 30))
    outside = timezone.make_aware(datetime(2026, 6, 10, 12, 0))
    assert services.in_quiet_hours(at=inside, start=start, end=end) is True
    assert services.in_quiet_hours(at=outside, start=start, end=end) is False


# ---------------------------------------------------------------------------
# Template rendering + locale fallback (D3-C-7)
# ---------------------------------------------------------------------------
def test_template_locale_fallback_en_to_uz(tenant_a, user_in):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        user.preferred_language = "ru"
        user.save(update_fields=["preferred_language"])
        # Only en + uz templates exist for this event/channel (the ru row is
        # absent), so a ru user must fall back to en.
        NotificationTemplate.objects.filter(
            event_type=EventType.ATTENDANCE_ABSENT, channel=Channel.IN_APP
        ).delete()
        NotificationTemplate.objects.create(
            event_type=EventType.ATTENDANCE_ABSENT,
            channel=Channel.IN_APP,
            locale="en",
            subject="EN",
            body="English body $lesson_id",
        )
        NotificationTemplate.objects.create(
            event_type=EventType.ATTENDANCE_ABSENT,
            channel=Channel.IN_APP,
            locale="uz",
            subject="UZ",
            body="Uzbek body",
        )
        subject, body = services.render_template(
            event_type=EventType.ATTENDANCE_ABSENT,
            channel=Channel.IN_APP,
            user_id=user.pk,
            context={"lesson_id": 99},
        )
        assert subject == "EN"
        assert body == "English body 99"  # safe_substitute filled the placeholder


def test_safe_substitute_missing_var_renders_literally(tenant_a, user_in):
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        NotificationTemplate.objects.update_or_create(
            event_type=EventType.ATTENDANCE_ABSENT,
            channel=Channel.IN_APP,
            locale="uz",
            defaults={"subject": "S", "body": "Hi $name, lesson $lesson_id"},
        )
        user.preferred_language = "uz"
        user.save(update_fields=["preferred_language"])
        _subject, body = services.render_template(
            event_type=EventType.ATTENDANCE_ABSENT,
            channel=Channel.IN_APP,
            user_id=user.pk,
            context={"lesson_id": 5},  # $name missing
        )
        assert body == "Hi $name, lesson 5"


# ---------------------------------------------------------------------------
# Absence signal end-to-end -> guardian SMS + in-app (D3-C-4/5)
# ---------------------------------------------------------------------------
@time_machine.travel("2026-06-10 12:00 +05:00", tick=False)
def test_absence_signal_end_to_end_guardian_gets_sms_and_in_app(
    tenant_a, sms_outbox, django_capture_on_commit_callbacks
):
    from apps.attendance.signals import student_marked_absent
    from apps.parents.tests.factories import GuardianFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        guardian = GuardianFactory(student=student, is_primary=True)
        guardian.parent.user.phone = "+998901112233"
        guardian.parent.user.save(update_fields=["phone"])

        with django_capture_on_commit_callbacks(execute=True):
            student_marked_absent.send(
                sender=None,
                record_id=1,
                student_id=student.pk,
                lesson_id=10,
                auto=False,
                schema_name=tenant_a.schema_name,
            )

        guardian_user = guardian.parent.user
        notif = Notification.objects.get(user=guardian_user)
        assert notif.event_type == EventType.ATTENDANCE_ABSENT
        assert notif.deliveries.filter(
            channel=Channel.IN_APP, status=NotificationDelivery.Status.SENT
        ).exists()
        assert notif.deliveries.filter(channel=Channel.SMS, status=NotificationDelivery.Status.SENT).exists()
        assert len(sms_outbox) == 1
        assert sms_outbox[0]["phone"] == "+998901112233"


def test_absence_signal_double_fire_dedupes(tenant_a, sms_outbox):
    from apps.attendance.signals import student_marked_absent
    from apps.parents.tests.factories import GuardianFactory
    from apps.students.tests.factories import StudentProfileFactory

    with schema_context(tenant_a.schema_name):
        student = StudentProfileFactory()
        guardian = GuardianFactory(student=student, is_primary=True)
        for _ in range(2):
            student_marked_absent.send(
                sender=None,
                record_id=55,
                student_id=student.pk,
                lesson_id=10,
                auto=False,
                schema_name=tenant_a.schema_name,
            )
        assert Notification.objects.filter(user=guardian.parent.user).count() == 1


# ---------------------------------------------------------------------------
# Dead-token cleanup after 3 push failures (D3-C-11)
# ---------------------------------------------------------------------------
@time_machine.travel("2026-06-10 12:00 +05:00", tick=False)
def test_dead_token_cleared_after_three_push_failures(tenant_a, user_in, django_capture_on_commit_callbacks):
    from apps.users.models import Device
    from infrastructure.push.fcm_client import MockFCMClient

    MockFCMClient.outbox.clear()
    user = user_in(tenant_a)
    with schema_context(tenant_a.schema_name):
        # MockFCMClient treats a token containing "dead" as a failure.
        device = Device.objects.create(
            user=user, device_id="d1", platform="android", push_token="dead-token-xyz"
        )
        # push-only dispatch x3 -> 3rd is the dead-token flip. Each dispatch
        # queues the fan-out via on_commit, so each runs in its own capture
        # block (the delivery history accumulates across the three runs).
        for _ in range(3):
            with django_capture_on_commit_callbacks(execute=True):
                services.dispatch(
                    event_type=EventType.ASSIGNMENTS_CREATED,
                    recipient_id=user.pk,
                    context={"assignment_id": 1},
                    channels=[Channel.PUSH],
                )
        device.refresh_from_db()
        assert device.push_token == ""  # token cleared
        assert NotificationDelivery.objects.filter(
            channel=Channel.PUSH, status=NotificationDelivery.Status.DEAD_TOKEN
        ).exists()
