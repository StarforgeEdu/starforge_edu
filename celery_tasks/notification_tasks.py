"""Celery tasks for notification dispatch (D3-C-5/10/11).

``dispatch_notification`` is the SINGLE channel fan-out and the ONLY producer of
``channel_layer.group_send`` (TD-15): it loads the Notification, resolves
per-channel preference + quiet hours, calls the adapters
(SMS/email/push/in-app+WS), and records each outcome as a NotificationDelivery
row. Idempotency: re-running for the same (notification, channel) does not create
a duplicate delivery; bounce handling clears a device push token after 3
consecutive push failures (counted from NotificationDelivery history).

Tasks are auto-registered with tenant-schemas-celery; pass ``_schema_name`` when
scheduling from a context that already knows the tenant.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from config.celery import app

logger = logging.getLogger("starforge.notifications")

# How many consecutive push failures for one device clears its token (D3-C-11).
PUSH_DEAD_TOKEN_THRESHOLD = 3


@app.task
@transaction.atomic
def dispatch_notification(notification_id: int, *, channels: list[str] | None = None) -> dict[str, Any]:
    """Resolve preferences + quiet hours and fan out to channels."""
    from apps.notifications.models import (
        Channel,
        Notification,
        NotificationDelivery,
    )
    from apps.notifications.services import (
        ALL_CHANNELS,
        channel_enabled_for_user,
        in_quiet_hours,
        quiet_hours_eta,
        render_template,
    )
    from apps.org.selectors import get_center_settings

    try:
        notification = (
            Notification.objects.select_for_update().select_related("user").get(pk=notification_id)
        )
    except Notification.DoesNotExist:
        logger.warning("dispatch_notification: notification %s gone", notification_id)
        return {"notification_id": notification_id, "status": "missing"}

    user = notification.user
    event_type = notification.event_type
    context = dict(notification.data or {})

    settings_obj = get_center_settings()
    now = timezone.now()
    quiet = in_quiet_hours(at=now, start=settings_obj.quiet_hours_start, end=settings_obj.quiet_hours_end)

    target_channels = [c for c in ALL_CHANNELS if (channels is None or c in channels)]
    results: dict[str, str] = {}

    for channel in target_channels:
        # Idempotent: an existing non-skip delivery means we already handled this
        # (notification, channel) — never double-send on a Celery retry.
        if _channel_is_complete(notification, channel):
            results[channel] = "already_handled"
            continue

        if not channel_enabled_for_user(user_id=user.pk, event_type=event_type, channel=channel):
            _record(notification, channel, NotificationDelivery.Status.SKIPPED_PREF)
            results[channel] = "skipped_pref"
            continue

        # Quiet hours: SMS + push deferred to window end; in-app + email immediate.
        if quiet and channel in (Channel.SMS, Channel.PUSH):
            # Idempotent deferral: a Celery redelivery of dispatch_notification
            # (at-least-once) re-enters this branch because the top-of-loop guard
            # EXCLUDES SKIPPED_QUIET_HOURS. Without this check a redelivery would
            # record a SECOND skip marker AND schedule a SECOND deliver_single_
            # channel -> two SMS/push for one event. If a skip marker already
            # exists for (notification, channel), the deferral is already queued.
            if NotificationDelivery.objects.filter(
                notification=notification,
                channel=channel,
                status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
            ).exists():
                results[channel] = "already_deferred"
                continue
            eta = quiet_hours_eta(at=now, end=settings_obj.quiet_hours_end)
            _record(
                notification,
                channel,
                NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
                provider_response={"deferred_to": eta.isoformat()},
            )
            from celery_tasks.notification_tasks import deliver_single_channel

            # ``deferred_to`` lets the deferred task detect when it is being run
            # BEFORE its scheduled eta. A real worker dequeues it at the eta, so
            # ``now >= deferred_to`` and it delivers; but Celery's eager mode
            # (tests) ignores ``eta`` and runs it immediately — there the task
            # must no-op so the SKIPPED_QUIET_HOURS marker survives (D3-C-8).
            deliver_single_channel.apply_async(
                kwargs={
                    "notification_id": notification.pk,
                    "channel": channel,
                    "deferred_to": eta.isoformat(),
                },
                eta=eta,
            )
            results[channel] = "deferred_quiet_hours"
            continue

        try:
            results[channel] = _deliver(notification, channel, context, render_template)
        except RetryableDeliveryError as exc:
            logger.warning(
                "notification %s channel %s needs retry (%s)",
                notification_id,
                channel,
                type(exc.__cause__ or exc).__name__,
            )
            results[channel] = _schedule_channel_retry(notification.pk, channel, attempt=1)
        except Exception as exc:
            # Persist a sanitized attempt record and continue the fan-out. A broken
            # provider must not suppress every later channel in ALL_CHANNELS.
            logger.warning(
                "notification %s channel %s failed (%s)",
                notification_id,
                channel,
                type(exc).__name__,
            )
            _record_retryable_failure(notification, channel, exc)
            results[channel] = _schedule_channel_retry(notification.pk, channel, attempt=1)

    return {"notification_id": notification_id, "results": results}


@app.task
@transaction.atomic
def deliver_single_channel(
    notification_id: int,
    channel: str,
    deferred_to: str | None = None,
    attempt: int = 0,
) -> str:
    """Deliver one channel for one notification (used for quiet-hours deferral).

    Clears the prior SKIPPED_QUIET_HOURS marker so the idempotency guard in
    ``dispatch_notification`` is not tripped by the deferred run.

    ``deferred_to`` is the ISO eta this task was scheduled for. A real worker
    dequeues it at (or after) the eta, so delivery proceeds. Celery's eager mode
    (tests) ignores the eta and runs the task immediately; running before the
    eta would clobber the SKIPPED_QUIET_HOURS deferral the contract requires, so
    we no-op until the window actually ends (D3-C-8).
    """
    from datetime import datetime

    from apps.notifications.models import Notification, NotificationDelivery
    from apps.notifications.services import render_template

    if deferred_to:
        scheduled = datetime.fromisoformat(deferred_to)
        if timezone.now() < scheduled:
            # Quiet window has not ended yet (eager run before the eta): leave the
            # deferral marker in place and let the scheduled run handle delivery.
            return "still_deferred"

    try:
        notification = (
            Notification.objects.select_for_update().select_related("user").get(pk=notification_id)
        )
    except Notification.DoesNotExist:
        return "missing"

    # Idempotency guard: a redelivery of this deferred task (or two skip markers
    # producing two scheduled tasks) must send only ONCE. If a non-skip delivery
    # already exists for (notification, channel), the window-end send already ran
    # — no-op. This complements the dispatch-side guard so the at-least-once
    # quiet-hours path never double-sends a paid SMS / push.
    if _channel_is_complete(notification, channel):
        return "already_delivered"

    NotificationDelivery.objects.filter(
        notification=notification,
        channel=channel,
        status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
    ).delete()
    context = dict(notification.data or {})
    try:
        return _deliver(notification, channel, context, render_template)
    except RetryableDeliveryError as exc:
        logger.warning(
            "notification %s channel %s retry %s failed (%s)",
            notification_id,
            channel,
            attempt,
            type(exc.__cause__ or exc).__name__,
        )
    except Exception as exc:
        logger.warning(
            "notification %s channel %s retry %s failed (%s)",
            notification_id,
            channel,
            attempt,
            type(exc).__name__,
        )
        _record_retryable_failure(notification, channel, exc)
    return _schedule_channel_retry(notification.pk, channel, attempt=attempt + 1)


def _deliver(notification, channel, context, render_template) -> str:
    """Route one channel; record the outcome; return a short status string."""
    from apps.notifications.models import Channel

    user = notification.user

    if channel == Channel.IN_APP:
        # In-app reuses the title/body rendered at dispatch (the in-app template).
        return _deliver_in_app(notification, notification.title, notification.body)

    # Other channels render their own channel-specific template (falling back to
    # the in-app text the dispatch stored when no channel template exists).
    subject, body = render_template(
        event_type=notification.event_type, channel=channel, user_id=user.pk, context=context
    )
    body = body or notification.body
    title = notification.title or subject

    if channel == Channel.SMS:
        return _deliver_sms(notification, user, body or title)
    if channel == Channel.EMAIL:
        return _deliver_email(notification, user, subject or title, body)
    if channel == Channel.PUSH:
        return _deliver_push(notification, user, title, body, context)
    return "unknown_channel"


class RetryableDeliveryError(RuntimeError):
    """A provider attempt failed after its per-recipient outcome was recorded."""


def _schedule_channel_retry(notification_id: int, channel: str, *, attempt: int) -> str:
    """Queue a committed retry without rolling back the failure attempt row."""
    if attempt > 5:
        return "failed"

    transaction.on_commit(
        lambda: deliver_single_channel.apply_async(
            kwargs={
                "notification_id": notification_id,
                "channel": channel,
                "attempt": attempt,
            },
            countdown=60,
        )
    )
    return "failed_retrying"


def _channel_is_complete(notification, channel: str) -> bool:
    """Return whether a retry has no unfinished destination for this channel.

    Push legitimately has multiple delivery rows (one per device), so a blanket
    unique constraint on ``(notification, channel)`` would corrupt its contract.
    The notification row lock serializes workers while this guard checks every
    active device independently.
    """
    from apps.notifications.models import Channel, NotificationDelivery

    rows = list(
        NotificationDelivery.objects.filter(notification=notification, channel=channel).values(
            "status", "provider_response"
        )
    )
    terminal = {
        NotificationDelivery.Status.SENT,
        NotificationDelivery.Status.SKIPPED_PREF,
        NotificationDelivery.Status.DEAD_TOKEN,
    }
    if channel != Channel.PUSH:
        return any(
            row["status"] in terminal
            or (
                row["status"] == NotificationDelivery.Status.FAILED
                and not (row["provider_response"] or {}).get("retryable", False)
            )
            for row in rows
        )

    if any(row["status"] == NotificationDelivery.Status.SKIPPED_PREF for row in rows):
        return True

    from apps.users.models import Device

    active_device_ids = set(
        Device.objects.filter(user=notification.user, revoked_at__isnull=True)
        .exclude(push_token="")
        .values_list("device_id", flat=True)
    )
    if not active_device_ids:
        return bool(rows)

    complete_device_ids = {
        response.get("device_id")
        for row in rows
        if (response := row["provider_response"] or {}).get("device_id")
        and (
            row["status"] in terminal
            or (
                row["status"] == NotificationDelivery.Status.FAILED
                and not response.get("retryable", False)
            )
        )
    }
    return active_device_ids.issubset(complete_device_ids)


def _record_retryable_failure(notification, channel: str, exc: Exception, **extra):
    """Persist only an exception class; provider messages may contain PII."""
    from apps.notifications.models import NotificationDelivery

    return _record(
        notification,
        channel,
        NotificationDelivery.Status.FAILED,
        provider_response={
            **extra,
            "error": type(exc).__name__,
            "retryable": True,
        },
    )


# ---------------------------------------------------------------------------
# Per-channel delivery
# ---------------------------------------------------------------------------
def _deliver_in_app(notification, title, body) -> str:
    """In-app = a delivery row + a WS group_send to {schema}.user.{id} (TD-15).

    The actual group_send is delegated to ``apps.notifications.services.
    push_in_app`` so the notifications stack has exactly ONE group_send producer
    call site (the producer-uniqueness grep test, D4-LC-6). The payload shape +
    schema-prefixed group naming live there (the Day-4 NotificationConsumer
    contract).
    """
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.notifications.services import push_in_app

    _record(notification, Channel.IN_APP, NotificationDelivery.Status.SENT)
    push_in_app(notification, title, body)
    return "sent"


def _deliver_sms(notification, user, text) -> str:
    from apps.notifications.models import Channel, NotificationDelivery
    from infrastructure.sms.eskiz_client import get_sms_client

    phone = getattr(user, "phone", None)
    if not phone:
        _record(
            notification,
            Channel.SMS,
            NotificationDelivery.Status.FAILED,
            provider_response={"error": "no_phone"},
        )
        return "failed_no_phone"
    response = get_sms_client().send(phone=phone, text=text)
    _record(notification, Channel.SMS, NotificationDelivery.Status.SENT, provider_response=response)
    return "sent"


def _deliver_email(notification, user, subject, body) -> str:
    from apps.notifications.models import Channel, NotificationDelivery
    from infrastructure.email.email_client import send_email

    email = getattr(user, "email", None)
    if not email:
        _record(
            notification,
            Channel.EMAIL,
            NotificationDelivery.Status.FAILED,
            provider_response={"error": "no_email"},
        )
        return "failed_no_email"
    send_email(to=email, subject=subject or notification.title, body=body)
    _record(notification, Channel.EMAIL, NotificationDelivery.Status.SENT)
    return "sent"


def _deliver_push(notification, user, title, body, context) -> str:
    """Send to every non-revoked device; clear a token after 3 consecutive fails."""
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.users.models import Device
    from infrastructure.push.fcm_client import get_push_client

    devices = list(Device.objects.filter(user=user, revoked_at__isnull=True).exclude(push_token=""))
    if not devices:
        _record(
            notification,
            Channel.PUSH,
            NotificationDelivery.Status.FAILED,
            provider_response={"error": "no_devices"},
        )
        return "failed_no_devices"

    client = get_push_client()
    any_sent = False
    any_dead = False
    retry_error: Exception | None = None
    for device in devices:
        if _push_device_is_complete(notification, device.device_id):
            continue
        try:
            response = client.send(
                token=device.push_token,
                title=title,
                body=body,
                data={k: str(v) for k, v in context.items()},
            )
        except Exception as exc:
            failure_status = NotificationDelivery.Status.FAILED
            if (
                _consecutive_push_failures(user_id=user.pk, device_id=device.device_id) + 1
                >= PUSH_DEAD_TOKEN_THRESHOLD
            ):
                Device.objects.filter(pk=device.pk).update(push_token="")
                failure_status = NotificationDelivery.Status.DEAD_TOKEN
                any_dead = True
            if failure_status == NotificationDelivery.Status.FAILED:
                _record_retryable_failure(
                    notification,
                    Channel.PUSH,
                    exc,
                    device_id=device.device_id,
                )
                retry_error = exc
            else:
                _record(
                    notification,
                    Channel.PUSH,
                    failure_status,
                    provider_response={
                        "device_id": device.device_id,
                        "error": type(exc).__name__,
                        "retryable": False,
                    },
                )
            continue
        if response.get("success"):
            any_sent = True
            _record(
                notification,
                Channel.PUSH,
                NotificationDelivery.Status.SENT,
                provider_response={"device_id": device.device_id, **response},
            )
        else:
            failure_status = NotificationDelivery.Status.FAILED
            if (
                _consecutive_push_failures(user_id=user.pk, device_id=device.device_id) + 1
                >= PUSH_DEAD_TOKEN_THRESHOLD
            ):
                # 3rd consecutive failure -> dead token: clear it + record dead_token.
                Device.objects.filter(pk=device.pk).update(push_token="")
                failure_status = NotificationDelivery.Status.DEAD_TOKEN
                any_dead = True
            _record(
                notification,
                Channel.PUSH,
                failure_status,
                provider_response={"device_id": device.device_id, **response},
            )
    if retry_error is not None:
        raise RetryableDeliveryError("push notification delivery failed") from retry_error
    if any_sent:
        return "sent"
    return "dead_token" if any_dead else "failed"


def _push_device_is_complete(notification, device_id: str) -> bool:
    from apps.notifications.models import Channel, NotificationDelivery

    rows = NotificationDelivery.objects.filter(
        notification=notification,
        channel=Channel.PUSH,
        provider_response__device_id=device_id,
    ).values("status", "provider_response")
    terminal = {
        NotificationDelivery.Status.SENT,
        NotificationDelivery.Status.DEAD_TOKEN,
    }
    return any(
        row["status"] in terminal
        or (
            row["status"] == NotificationDelivery.Status.FAILED
            and not (row["provider_response"] or {}).get("retryable", False)
        )
        for row in rows
    )


def _consecutive_push_failures(*, user_id: int, device_id: str) -> int:
    """Count trailing consecutive push failures for one device (newest first).

    A SENT (or DEAD_TOKEN, which already cleared the token) breaks the streak.
    """
    from apps.notifications.models import Channel, NotificationDelivery

    recent = (
        NotificationDelivery.objects.filter(
            channel=Channel.PUSH,
            notification__user_id=user_id,
            provider_response__device_id=device_id,
        )
        .order_by("-created_at")
        .values_list("status", flat=True)[:PUSH_DEAD_TOKEN_THRESHOLD]
    )
    streak = 0
    for status in recent:
        if status == NotificationDelivery.Status.FAILED:
            streak += 1
        else:
            break
    return streak


def _record(notification, channel, status, *, provider_response: dict | None = None):
    from apps.notifications.models import NotificationDelivery

    return NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
        status=status,
        provider_response=provider_response or {},
        sent_at=timezone.now() if status == NotificationDelivery.Status.SENT else None,
    )


# ---------------------------------------------------------------------------
# Cohort announcements (D3-C-10) — chunked, rate-limited
# ---------------------------------------------------------------------------
@app.task(rate_limit="25/s")
def announce_cohort_chunk(
    *, user_ids: list[int], announcement_id: str, title: str, body: str, context: dict
) -> int:
    """Dispatch one announcement to a chunk of users (dedupe per (announcement,user))."""
    from apps.notifications.models import EventType
    from apps.notifications.services import dispatch

    sent = 0
    for uid in user_ids:
        result = dispatch(
            event_type=EventType.COHORTS_ANNOUNCEMENT,
            recipient_id=uid,
            context={"title": title, "body": body, **context},
            dedupe_key=f"cohorts.announcement:{announcement_id}:{uid}",
        )
        if result is not None:
            sent += 1
    return sent


@app.task(rate_limit="25/s")
def dispatch_many_chunk(
    *, user_ids: list[int], event_type: str, context: dict, dedupe_prefix: str | None = None
) -> int:
    """Dispatch one event to a chunk of recipients off the request thread (D3-C).

    The offloaded form of receivers._dispatch_many for LARGE cohort fan-outs
    (lesson reschedule/cancel, assignment publish): looping dispatch() inline in the
    triggering HTTP request costs O(recipients) x ~3-4 queries each, saturating a DB
    connection and timing out a bulk reschedule for a big cohort. Same dedupe contract
    as the inline path (`{dedupe_prefix}:{uid}`)."""
    from apps.notifications.services import dispatch

    sent = 0
    for uid in user_ids:
        dedupe_key = f"{dedupe_prefix}:{uid}" if dedupe_prefix else None
        if dispatch(event_type=event_type, recipient_id=uid, context=context, dedupe_key=dedupe_key) is not None:
            sent += 1
    return sent
