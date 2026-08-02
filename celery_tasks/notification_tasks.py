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

import json
import logging
from datetime import datetime, timedelta
from typing import Any, TypedDict, cast

from django.db import transaction
from django.utils import timezone

from config.celery import app

logger = logging.getLogger("starforge.notifications")

# How many consecutive push failures for one device clears its token (D3-C-11).
PUSH_DEAD_TOKEN_THRESHOLD = 3

# One bulk schedule HTTP commit publishes one small coordinator message.  The
# coordinator then streams recipients and creates bounded child jobs; no child can
# perform an unbounded lessons x recipients loop or carry an unbounded broker payload.
LESSON_RESCHEDULE_MAX_EVENTS = 1_000
LESSON_RESCHEDULE_EVENTS_PER_TASK = 5
LESSON_RESCHEDULE_RECIPIENTS_PER_TASK = 50
DEFERRED_DELIVERY_BATCH_SIZE = 500
DEFERRED_DELIVERY_LEASE = timedelta(minutes=5)
PROVIDER_CLAIM_BATCH_SIZE = 500
# Provider timeouts are measured in seconds. A much longer claim lease avoids
# racing an ordinarily slow request while still surfacing a killed worker in a
# bounded period. Unknown claims are never retried without explicit evidence.
PROVIDER_CLAIM_STALE_AFTER = timedelta(minutes=15)
RECONCILED_RETRY_LEASE = timedelta(minutes=5)


class RescheduleMove(TypedDict):
    lesson_id: int
    old_start: str
    moved_at: str
    move_id: str


class StudentRecipient(TypedDict):
    user_id: int
    principal_kind: str
    principal_id: int


_GUARDIAN_RELATION_EVENTS = frozenset(
    {
        "attendance.absent",
        "attendance.late",
        "academics.grades_published",
        "assignments.graded",
        "students.enrollment_changed",
        "finance.invoice_issued",
        "finance.payment_reminder",
        "payments.payment_completed",
        "payments.payment_failed",
    }
)
_PRIMARY_GUARDIAN_EVENTS = frozenset(
    {
        "finance.invoice_issued",
        "finance.payment_reminder",
        "payments.payment_completed",
        "payments.payment_failed",
    }
)
_PUSH_ROUTING_CONTEXT_KEYS = frozenset(
    {
        "assignment_id",
        "cohort_id",
        "cover_id",
        "invoice_id",
        "job_id",
        "lesson_id",
        "message_id",
        "notification_id",
        "payment_id",
        "penalty_id",
        "request_id",
        "run_id",
        "student_id",
        "submission_id",
        "thread_id",
    }
)


@app.task
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
        operator_channel_enabled,
        quiet_hours_eta,
        render_template,
    )
    from apps.org.selectors import get_center_settings

    try:
        notification = Notification.objects.select_related("user").get(pk=notification_id)
    except Notification.DoesNotExist:
        logger.warning("dispatch_notification: notification %s gone", notification_id)
        return {"notification_id": notification_id, "status": "missing"}

    # Additive-migration/rolling-deploy guard: old rows and any creation path
    # that cannot prove a role-native recipient remain durable but may never
    # fan out through in-app, SMS, email, push, or WebSocket channels.
    principal_kind = notification.recipient_principal_kind
    principal_id = notification.recipient_principal_id
    if not notification.is_deliverable or principal_kind is None or principal_id is None:
        return {"notification_id": notification_id, "status": "quarantined"}
    if not _lock_live_recipient(notification):
        return {"notification_id": notification_id, "status": "recipient_inactive"}

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

        if not operator_channel_enabled(channel):
            _record(
                notification,
                channel,
                NotificationDelivery.Status.SKIPPED_DISABLED,
                provider_response={"reason": "operator_disabled"},
            )
            results[channel] = "skipped_disabled"
            continue

        if not channel_enabled_for_user(
            user_id=user.pk,
            recipient_principal_kind=principal_kind,
            recipient_principal_id=principal_id,
            event_type=event_type,
            channel=channel,
        ):
            _record(notification, channel, NotificationDelivery.Status.SKIPPED_PREF)
            results[channel] = "skipped_pref"
            continue

        # Quiet hours: SMS + push deferred to window end; in-app + email immediate.
        if quiet and channel in (Channel.SMS, Channel.PUSH):
            # Idempotent deferral: a Celery redelivery of dispatch_notification
            # (at-least-once) re-enters this branch because the top-of-loop guard
            # EXCLUDES SKIPPED_QUIET_HOURS. Without this check a redelivery would
            # record a SECOND durable marker. If one already exists, the
            # reconciliation sweep already has everything needed to deliver it.
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
            # Do not put an hours-long ETA message into Redis. Celery reserves
            # ETA jobs in worker memory, which both delays crash recovery and
            # makes it impossible to prove an empty broker before a schema
            # cutover. The durable database marker is picked up by the bounded
            # reconciliation sweep once the quiet window ends.
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
        except ProviderOutcomeUnknown:
            # The durable pre-send claim proves the provider may have accepted
            # this contact. Automatic retry could duplicate an SMS/email/push.
            results[channel] = "provider_outcome_unknown"
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
def reconcile_deferred_notification_deliveries() -> int:
    """Fan out the durable quiet-hours outbox to each active tenant."""

    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    schemas = list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .order_by("schema_name")
        .values_list("schema_name", flat=True)
    )
    for schema in schemas:
        reconcile_deferred_notification_deliveries_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def reconcile_deferred_notification_deliveries_for_schema() -> int:
    """Lease and enqueue one bounded batch of due deferred deliveries.

    The skip row is the outbox. ``last_enqueued_at`` is a short lease rather
    than a completion flag: a broker publish failure or lost delivery becomes
    eligible again automatically, while duplicate sweep runs remain harmless
    because ``deliver_single_channel`` serializes on the Notification row.
    """

    from django.db.models import DateTimeField
    from django.db.models.fields.json import KeyTextTransform
    from django.db.models.functions import Cast

    from apps.notifications.models import NotificationDelivery
    from core.utils import current_schema

    now = timezone.now()
    retry_before = now - DEFERRED_DELIVERY_LEASE
    # Values are emitted only by this module as offset-aware ISO-8601 strings.
    # Casting in PostgreSQL gives correct chronological comparisons across
    # organization timezones instead of comparing JSON strings lexically.
    # django-stubs retains the annotated model type through values_list(); the
    # runtime queryset is explicitly the flat integer primary-key projection.
    due_ids = cast(
        list[int],
        list(
            NotificationDelivery.objects.filter(
                status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
                provider_response__has_key="deferred_to",
            )
            .annotate(
                deferred_at=Cast(
                    KeyTextTransform("deferred_to", "provider_response"),
                    output_field=DateTimeField(),
                )
            )
            .filter(deferred_at__lte=now)
            .order_by("deferred_at", "pk")
            .values_list("pk", flat=True)[:DEFERRED_DELIVERY_BATCH_SIZE]
        ),
    )
    schema = current_schema()
    queued = 0
    for delivery_id in due_ids:
        with transaction.atomic():
            delivery = (
                NotificationDelivery.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=delivery_id,
                    status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
                )
                .first()
            )
            if delivery is None:
                continue
            evidence = dict(delivery.provider_response or {})
            deferred_to = _offset_timestamp(evidence.get("deferred_to"), field="deferred_to")
            if datetime.fromisoformat(deferred_to) > now:
                continue
            raw_last_enqueued = evidence.get("last_enqueued_at")
            if raw_last_enqueued:
                try:
                    last_enqueued = datetime.fromisoformat(
                        _offset_timestamp(raw_last_enqueued, field="last_enqueued_at")
                    )
                except ValueError:
                    # Corrupt lease evidence must not permanently strand a valid
                    # durable deferral; replace it with a fresh trusted value.
                    last_enqueued = None
                if last_enqueued is not None and last_enqueued > retry_before:
                    continue
            evidence["last_enqueued_at"] = now.isoformat()
            delivery.provider_response = evidence
            delivery.save(update_fields=["provider_response"])

            notification_id = delivery.notification_id
            channel = delivery.channel

            def enqueue(
                *,
                notification_id: int = notification_id,
                channel: str = channel,
                deferred_to: str = deferred_to,
                schema: str = schema,
            ) -> None:
                deliver_single_channel.delay(
                    notification_id,
                    channel,
                    deferred_to=deferred_to,
                    _schema_name=schema,
                )

            transaction.on_commit(enqueue)
            queued += 1
    return queued


@app.task
def reconcile_stale_provider_delivery_claims() -> int:
    """Fan out stale-claim reconciliation to every active tenant."""

    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center

    schemas = list(
        Center.objects.filter(is_active=True)
        .exclude(schema_name=get_public_schema_name())
        .order_by("schema_name")
        .values_list("schema_name", flat=True)
    )
    for schema in schemas:
        reconcile_stale_provider_delivery_claims_for_schema.delay(_schema_name=schema)
    return len(schemas)


@app.task
def reconcile_stale_provider_delivery_claims_for_schema() -> int:
    """Move abandoned pre-send claims to an operator-reviewable unknown state.

    A worker can die after a provider accepted a message but before the success
    update commits. Retrying such a row would create a duplicate contact. This
    bounded, skip-locked sweep preserves that ambiguity explicitly; only the
    evidence-backed management command may later resolve it.
    """

    from apps.notifications.models import NotificationDelivery

    now = timezone.now()
    cutoff = now - PROVIDER_CLAIM_STALE_AFTER
    claim_ids = list(
        NotificationDelivery.objects.filter(
            channel__in=("sms", "email", "push"),
            status=NotificationDelivery.Status.CLAIMED,
            created_at__lte=cutoff,
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:PROVIDER_CLAIM_BATCH_SIZE]
    )
    reconciled = 0
    for claim_id in claim_ids:
        with transaction.atomic():
            claim = (
                NotificationDelivery.objects.select_for_update(skip_locked=True)
                .filter(
                    channel__in=("sms", "email", "push"),
                    pk=claim_id,
                    status=NotificationDelivery.Status.CLAIMED,
                    created_at__lte=cutoff,
                )
                .first()
            )
            if claim is None:
                continue
            evidence = dict(claim.provider_response or {})
            evidence.update(
                {
                    "unknown_at": now.isoformat(),
                    "unknown_reason": "stale_claim",
                    "reconciliation_required": True,
                }
            )
            claim.status = NotificationDelivery.Status.UNKNOWN
            claim.provider_response = evidence
            claim.save(update_fields=["status", "provider_response"])
            reconciled += 1
    return reconciled + _enqueue_reconciled_provider_retries()


def _enqueue_reconciled_provider_retries() -> int:
    """Lease durable, explicitly authorized retry intents for broker delivery."""

    from apps.notifications.models import NotificationDelivery
    from core.utils import current_schema

    now = timezone.now()
    lease_cutoff = now - RECONCILED_RETRY_LEASE
    marker_ids = list(
        NotificationDelivery.objects.filter(
            status=NotificationDelivery.Status.FAILED,
            provider_response__retryable=True,
            provider_response__has_key="retry_requested_at",
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[:PROVIDER_CLAIM_BATCH_SIZE]
    )
    queued = 0
    schema = current_schema()
    for marker_id in marker_ids:
        with transaction.atomic():
            marker = (
                NotificationDelivery.objects.select_for_update(skip_locked=True)
                .filter(
                    pk=marker_id,
                    status=NotificationDelivery.Status.FAILED,
                    provider_response__retryable=True,
                )
                .first()
            )
            if marker is None:
                continue
            evidence = dict(marker.provider_response or {})
            if (
                NotificationDelivery.objects.filter(
                    notification_id=marker.notification_id,
                    channel=marker.channel,
                    delivery_key=marker.delivery_key,
                    status__in=(
                        NotificationDelivery.Status.CLAIMED,
                        NotificationDelivery.Status.UNKNOWN,
                        NotificationDelivery.Status.SENT,
                    ),
                )
                .exclude(pk=marker.pk)
                .exists()
            ):
                evidence.update({"retryable": False, "retry_resolved_at": now.isoformat()})
                marker.provider_response = evidence
                marker.save(update_fields=["provider_response"])
                continue
            raw_lease = evidence.get("retry_last_enqueued_at")
            if raw_lease:
                try:
                    leased_at = datetime.fromisoformat(str(raw_lease))
                except ValueError:
                    leased_at = None
                if leased_at is not None and leased_at > lease_cutoff:
                    continue
            evidence["retry_last_enqueued_at"] = now.isoformat()
            marker.provider_response = evidence
            marker.save(update_fields=["provider_response"])
            notification_id = marker.notification_id
            channel = marker.channel
            transaction.on_commit(
                lambda notification_id=notification_id, channel=channel: deliver_single_channel.delay(
                    notification_id,
                    channel,
                    _schema_name=schema,
                )
            )
            queued += 1
    return queued


@app.task
def deliver_single_channel(
    notification_id: int,
    channel: str,
    deferred_to: str | None = None,
    attempt: int = 0,
) -> str:
    """Deliver one channel for one notification (used for quiet-hours deferral).

    Clears the prior SKIPPED_QUIET_HOURS marker so the idempotency guard in
    ``dispatch_notification`` is not tripped by the deferred run.

    ``deferred_to`` is the ISO due time claimed from the durable outbox marker.
    A defensive time check protects against clock skew or a stale duplicate
    sweep; running early must not clobber the quiet-hours marker.
    """
    from datetime import datetime

    from apps.notifications.models import Notification, NotificationDelivery
    from apps.notifications.services import (
        ALL_CHANNELS,
        channel_enabled_for_user,
        operator_channel_enabled,
        render_template,
    )

    if deferred_to:
        scheduled = datetime.fromisoformat(deferred_to)
        if timezone.now() < scheduled:
            # Quiet window has not ended yet: leave the marker in place so the
            # next reconciliation sweep can claim it at the correct time.
            return "still_deferred"

    try:
        notification = Notification.objects.select_related("user").get(pk=notification_id)
    except Notification.DoesNotExist:
        return "missing"
    if not notification.is_deliverable:
        return "quarantined"
    principal_kind = notification.recipient_principal_kind
    principal_id = notification.recipient_principal_id
    if principal_kind is None or principal_id is None:
        return "quarantined"
    if not _lock_live_recipient(notification):
        return "recipient_inactive"
    if channel not in ALL_CHANNELS:
        return "invalid_channel"

    # Idempotency guard: a redelivery of this deferred task (or two skip markers
    # producing two scheduled tasks) must send only ONCE. If a non-skip delivery
    # already exists for (notification, channel), the window-end send already ran
    # — no-op. This complements the dispatch-side guard so the at-least-once
    # quiet-hours path never double-sends a paid SMS / push.
    if _channel_is_complete(notification, channel):
        return "already_delivered"

    if not operator_channel_enabled(channel):
        NotificationDelivery.objects.filter(
            notification=notification,
            channel=channel,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
        ).delete()
        _record(
            notification,
            channel,
            NotificationDelivery.Status.SKIPPED_DISABLED,
            provider_response={"reason": "operator_disabled"},
        )
        return "skipped_disabled"

    # A quiet-hours task or provider retry can run long after the initial
    # dispatch. Respect an opt-out made in that interval before any external
    # contact, and make the decision terminal for subsequent retry attempts.
    if not channel_enabled_for_user(
        user_id=notification.user_id,
        recipient_principal_kind=principal_kind,
        recipient_principal_id=principal_id,
        event_type=notification.event_type,
        channel=channel,
    ):
        NotificationDelivery.objects.filter(
            notification=notification,
            channel=channel,
            status=NotificationDelivery.Status.SKIPPED_QUIET_HOURS,
        ).delete()
        _record(notification, channel, NotificationDelivery.Status.SKIPPED_PREF)
        return "skipped_pref"

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
    except ProviderOutcomeUnknown:
        return "provider_outcome_unknown"
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
        return _deliver_sms(notification, body or title)
    if channel == Channel.EMAIL:
        return _deliver_email(notification, subject or title, body)
    if channel == Channel.PUSH:
        return _deliver_push(notification, user, title, body, context)
    return "unknown_channel"


class RetryableDeliveryError(RuntimeError):
    """A provider attempt failed after its per-recipient outcome was recorded."""


class ProviderOutcomeUnknown(RuntimeError):
    """Provider contact began but its acceptance could not be proved."""


def _lock_live_recipient(notification, *, lock: bool = False) -> bool:
    """Revalidate the exact role account before delivery.

    Attribution is an immutable historical snapshot, but delivery authorization
    is live. Provider claims call this with ``lock=True`` in the same short
    transaction that persists the claim. The external network request happens
    only after that transaction commits.
    """

    if not notification.user.is_active:
        return False
    from django.apps import apps as django_apps

    labels = {
        "student": "students.StudentProfile",
        "teacher": "teachers.TeacherProfile",
        "parent": "parents.ParentProfile",
        "staff": "org.StaffProfile",
    }
    label = labels.get(notification.recipient_principal_kind)
    if label is None or notification.recipient_principal_id is None:
        return False
    model = django_apps.get_model(label)
    profiles = model.objects.select_for_update() if lock else model.objects
    profile_is_live = profiles.filter(
        pk=notification.recipient_principal_id,
        user_id=notification.user_id,
        is_active=True,
    ).exists()
    if not profile_is_live:
        return False
    if (
        notification.recipient_principal_kind != "parent"
        or notification.event_type not in _GUARDIAN_RELATION_EVENTS
    ):
        return True

    # A guardian may be removed by the family/court workflow after a quiet-hour
    # notification was queued. Re-check the exact relationship under a row lock
    # immediately before sending; historical attribution alone is not authority
    # to contact a revoked guardian.
    raw_student_id = (notification.data or {}).get("student_id")
    if not isinstance(raw_student_id, int) or isinstance(raw_student_id, bool) or raw_student_id <= 0:
        return False
    from apps.parents.models import Guardian

    guardians = Guardian.objects.all()
    if lock:
        guardians = guardians.select_for_update()
    guardians = guardians.filter(
        parent_id=notification.recipient_principal_id,
        student_id=raw_student_id,
        revoked_at__isnull=True,
    )
    if notification.event_type in _PRIMARY_GUARDIAN_EVENTS:
        guardians = guardians.filter(is_primary=True)
    return guardians.exists()


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
    External provider contact is serialized by the durable partial-unique claim
    rather than by holding a database transaction open across a network call.
    """
    from apps.notifications.models import Channel, NotificationDelivery

    rows = list(
        NotificationDelivery.objects.filter(notification=notification, channel=channel).values(
            "status", "provider_response"
        )
    )
    terminal = {
        NotificationDelivery.Status.CLAIMED,
        NotificationDelivery.Status.UNKNOWN,
        NotificationDelivery.Status.SENT,
        NotificationDelivery.Status.SKIPPED_PREF,
        NotificationDelivery.Status.SKIPPED_DISABLED,
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

    if any(
        row["status"]
        in (NotificationDelivery.Status.SKIPPED_PREF, NotificationDelivery.Status.SKIPPED_DISABLED)
        for row in rows
    ):
        return True

    active_device_ids = set(_active_push_devices(notification).values_list("device_id", flat=True))
    if not active_device_ids:
        return bool(rows)

    complete_device_ids = {
        response.get("device_id")
        for row in rows
        if (response := row["provider_response"] or {}).get("device_id")
        and (
            row["status"] in terminal
            or (row["status"] == NotificationDelivery.Status.FAILED and not response.get("retryable", False))
        )
    }
    return active_device_ids.issubset(complete_device_ids)


def _active_push_devices(notification):
    """Return devices backed only by this exact live role-principal session.

    A token can outlive logout, password reset, or an expired app session. Push
    must follow the same authorization boundary as the API, so a registered
    token alone is never sufficient to receive private notification content.
    """
    from django.db.models import Exists, OuterRef

    from apps.users.models import Device, Session
    from core.session_auth import session_idle_timeout

    now = timezone.now()
    idle_cutoff = now - session_idle_timeout()
    exact_sessions = Session.objects.filter(
        user_id=notification.user_id,
        device_id=OuterRef("device_id"),
        principal_kind=notification.recipient_principal_kind,
        principal_id=notification.recipient_principal_id,
        revoked_at__isnull=True,
        expires_at__gt=now,
        last_used_at__gte=idle_cutoff,
        read_only=False,
    )
    other_role_sessions = Session.objects.filter(
        user_id=notification.user_id,
        device_id=OuterRef("device_id"),
        revoked_at__isnull=True,
        expires_at__gt=now,
        last_used_at__gte=idle_cutoff,
    ).exclude(
        principal_kind=notification.recipient_principal_kind,
        principal_id=notification.recipient_principal_id,
    )
    return (
        Device.objects.filter(
            user_id=notification.user_id,
            user__is_active=True,
            revoked_at__isnull=True,
        )
        .exclude(push_token="")
        .annotate(
            has_exact_session=Exists(exact_sessions),
            has_other_role_session=Exists(other_role_sessions),
        )
        .filter(has_exact_session=True, has_other_role_session=False)
    )


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
    """Persist the feed outcome and push to the exact role-principal WS group.

    The actual group_send is delegated to ``apps.notifications.services.
    push_in_app`` so the notifications stack has exactly ONE group_send producer
    call site (the producer-uniqueness grep test, D4-LC-6). The payload shape +
    schema-prefixed group naming live there (the Day-4 NotificationConsumer
    contract).
    """
    from django.db import IntegrityError

    from apps.notifications.models import Channel, NotificationDelivery
    from apps.notifications.services import push_in_app

    try:
        _record(
            notification,
            Channel.IN_APP,
            NotificationDelivery.Status.SENT,
            delivery_key="feed",
        )
    except IntegrityError:
        if NotificationDelivery.objects.filter(
            notification=notification,
            channel=Channel.IN_APP,
            delivery_key="feed",
            status=NotificationDelivery.Status.SENT,
        ).exists():
            return "already_claimed"
        raise
    push_in_app(notification, title, body)
    return "sent"


def _deliver_sms(notification, text) -> str:
    from apps.notifications.models import Channel, NotificationDelivery
    from infrastructure.sms.eskiz_client import get_sms_client

    phone = _role_contact(notification, "phone")
    if not phone:
        _record(
            notification,
            Channel.SMS,
            NotificationDelivery.Status.FAILED,
            provider_response={"error": "no_phone"},
        )
        return "failed_no_phone"
    claim = _claim_provider_delivery(notification, Channel.SMS, "recipient")
    if claim is None:
        return "already_claimed"
    try:
        response = get_sms_client().send(
            phone=phone,
            text=_bounded_utf8(text, max_bytes=8 * 1024, max_chars=4096),
        )
    except BaseException as exc:
        _mark_provider_unknown(claim, exc)
        raise ProviderOutcomeUnknown from exc
    _complete_provider_delivery(claim, _safe_sms_provider_response(response))
    return "sent"


def _deliver_email(notification, subject, body) -> str:
    from apps.notifications.models import Channel, NotificationDelivery
    from infrastructure.email.email_client import send_email

    email = _role_contact(notification, "email")
    if not email:
        _record(
            notification,
            Channel.EMAIL,
            NotificationDelivery.Status.FAILED,
            provider_response={"error": "no_email"},
        )
        return "failed_no_email"
    claim = _claim_provider_delivery(notification, Channel.EMAIL, "recipient")
    if claim is None:
        return "already_claimed"
    try:
        send_email(
            to=email,
            subject=_bounded_utf8(subject or notification.title, max_bytes=1024, max_chars=255),
            body=_bounded_utf8(body, max_bytes=64 * 1024, max_chars=64 * 1024),
        )
    except BaseException as exc:
        _mark_provider_unknown(claim, exc)
        raise ProviderOutcomeUnknown from exc
    _complete_provider_delivery(claim, {})
    return "sent"


def _safe_sms_provider_response(response: object) -> dict:
    """Persist only bounded non-PII provider receipt fields."""

    raw = response if isinstance(response, dict) else {}

    def bounded(name: str, limit: int) -> str:
        value = str(raw.get(name) or "")
        return "".join(char for char in value if char >= " " and char != "\x7f")[:limit]

    return {
        "status": bounded("status", 32) or None,
        "id": bounded("id", 128) or None,
        "message_id": bounded("message_id", 128) or None,
        "mock": bool(raw.get("mock", False)),
    }


def _deliver_push(notification, user, title, body, context) -> str:
    """Send to devices with a live session; clear dead provider tokens."""
    from apps.notifications.models import Channel, NotificationDelivery
    from apps.users.models import Device
    from core.utils import current_schema
    from infrastructure.push.fcm_client import get_push_client

    devices = list(_active_push_devices(notification))
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
    any_unknown = False
    for device in devices:
        if _push_device_is_complete(notification, device.device_id):
            continue
        claim = _claim_provider_delivery(
            notification,
            Channel.PUSH,
            f"device:{device.device_id}",
            device_id=device.device_id,
        )
        if claim is None:
            continue
        try:
            response = client.send(
                token=device.push_token,
                title=_bounded_utf8(title, max_bytes=512, max_chars=255),
                body=_bounded_utf8(body, max_bytes=2048, max_chars=2048),
                data=_bounded_push_data(notification, context, tenant_slug=current_schema()),
            )
        except Exception as exc:
            # Dependency, credential, timeout, and provider outages say nothing
            # about token validity. Never erase a token for a generic exception.
            _mark_provider_unknown(claim, exc)
            any_unknown = True
            continue
        if response.get("success"):
            any_sent = True
            _complete_provider_delivery(
                claim,
                _safe_push_provider_response(response, device_id=device.device_id),
            )
        elif response.get("error") == "unregistered":
            failure_status = NotificationDelivery.Status.FAILED
            if (
                _consecutive_push_failures(
                    user_id=user.pk,
                    recipient_principal_kind=notification.recipient_principal_kind,
                    recipient_principal_id=notification.recipient_principal_id,
                    device_id=device.device_id,
                )
                + 1
                >= PUSH_DEAD_TOKEN_THRESHOLD
            ):
                # 3rd consecutive failure -> dead token: clear it + record dead_token.
                Device.objects.filter(pk=device.pk).update(push_token="")
                failure_status = NotificationDelivery.Status.DEAD_TOKEN
                any_dead = True
            _finish_provider_delivery(
                claim,
                failure_status,
                _safe_push_provider_response(response, device_id=device.device_id),
            )
        else:
            # The generic adapter cannot prove whether a transport error
            # happened before or after provider acceptance.
            _mark_provider_unknown(claim, RuntimeError("indeterminate provider response"))
            any_unknown = True
    if any_unknown:
        return "partially_unknown" if any_sent or any_dead else "provider_outcome_unknown"
    if any_sent:
        return "sent"
    return "dead_token" if any_dead else "failed"


def _safe_push_provider_response(response: object, *, device_id: str) -> dict:
    """Persist a small allowlist, never an arbitrary provider response body."""

    raw = response if isinstance(response, dict) else {}

    def bounded(name: str, limit: int) -> str:
        value = str(raw.get(name) or "")
        return "".join(char for char in value if char >= " " and char != "\x7f")[:limit]

    return {
        "device_id": str(device_id)[:128],
        "success": bool(raw.get("success", False)),
        "message_id": bounded("message_id", 128) or None,
        "error": bounded("error", 64) or None,
        "mock": bool(raw.get("mock", False)),
    }


def _bounded_push_data(notification, context: dict, *, tenant_slug: str) -> dict[str, str]:
    """Build an FCM data map below its small platform payload budget."""

    data = {
        "event_type": notification.event_type,
        "notification_id": str(notification.pk),
        "tenant_slug": str(tenant_slug),
    }
    for key, value in context.items():
        # Never copy arbitrary domain context into a third-party push payload.
        # It may contain PII, internal notes, or short-lived signed download
        # credentials. Reviewed opaque routing ids are sufficient for the app
        # to refetch the exact authorized notification after opening the push.
        if key in data or key not in _PUSH_ROUTING_CONTEXT_KEYS:
            continue
        candidate = {
            **data,
            key: _bounded_utf8(value, max_bytes=256, max_chars=256),
        }
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode()) > 3072:
            break
        data = candidate
    return data


def _bounded_utf8(value: object, *, max_bytes: int, max_chars: int) -> str:
    text = str(value)[:max_chars]
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _role_contact(notification, field: str) -> str:
    """Resolve contact data from the immutable exact role-native recipient."""

    from django.apps import apps as django_apps

    labels = {
        "student": "students.StudentProfile",
        "teacher": "teachers.TeacherProfile",
        "parent": "parents.ParentProfile",
        "staff": "org.StaffProfile",
    }
    label = labels.get(notification.recipient_principal_kind)
    if label is None:
        return ""
    model = django_apps.get_model(label)
    value = (
        model.objects.filter(
            pk=notification.recipient_principal_id,
            user_id=notification.user_id,
            is_active=True,
        )
        .values_list(field, flat=True)
        .first()
    )
    return str(value or "").strip()


def _push_device_is_complete(notification, device_id: str) -> bool:
    from apps.notifications.models import Channel, NotificationDelivery

    rows = NotificationDelivery.objects.filter(
        notification=notification,
        channel=Channel.PUSH,
        provider_response__device_id=device_id,
    ).values("status", "provider_response")
    terminal = {
        NotificationDelivery.Status.CLAIMED,
        NotificationDelivery.Status.UNKNOWN,
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


def _consecutive_push_failures(
    *,
    user_id: int,
    recipient_principal_kind: str,
    recipient_principal_id: int,
    device_id: str,
) -> int:
    """Count trailing consecutive push failures for one device (newest first).

    A SENT (or DEAD_TOKEN, which already cleared the token) breaks the streak.
    """
    from apps.notifications.models import Channel, NotificationDelivery

    recent = (
        NotificationDelivery.objects.filter(
            channel=Channel.PUSH,
            notification__user_id=user_id,
            notification__recipient_principal_kind=recipient_principal_kind,
            notification__recipient_principal_id=recipient_principal_id,
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


def _record(
    notification,
    channel,
    status,
    *,
    provider_response: dict | None = None,
    delivery_key: str | None = None,
):
    from apps.notifications.models import NotificationDelivery

    return NotificationDelivery.objects.create(
        notification=notification,
        channel=channel,
        status=status,
        delivery_key=delivery_key,
        provider_response=provider_response or {},
        sent_at=timezone.now() if status == NotificationDelivery.Status.SENT else None,
    )


def _claim_provider_delivery(notification, channel: str, delivery_key: str, **evidence):
    """Commit a unique opaque claim before any external provider contact."""
    from django.db import IntegrityError

    from apps.notifications.models import NotificationDelivery

    try:
        with transaction.atomic():
            locked_notification = (
                type(notification).objects.select_for_update().select_related("user").get(pk=notification.pk)
            )
            if not locked_notification.is_deliverable or not _lock_live_recipient(
                locked_notification,
                lock=True,
            ):
                return None
            return NotificationDelivery.objects.create(
                notification=locked_notification,
                channel=channel,
                status=NotificationDelivery.Status.CLAIMED,
                delivery_key=delivery_key[:160],
                provider_response={**evidence, "claimed_at": timezone.now().isoformat()},
            )
    except IntegrityError:
        if NotificationDelivery.objects.filter(
            notification=notification,
            channel=channel,
            delivery_key=delivery_key[:160],
            status__in=(
                NotificationDelivery.Status.CLAIMED,
                NotificationDelivery.Status.UNKNOWN,
                NotificationDelivery.Status.SENT,
            ),
        ).exists():
            return None
        raise


def _finish_provider_delivery(claim, status: str, response: dict) -> None:
    from apps.notifications.models import NotificationDelivery

    updated = NotificationDelivery.objects.filter(
        pk=claim.pk,
        status=NotificationDelivery.Status.CLAIMED,
    ).update(
        status=status,
        provider_response=response,
        sent_at=timezone.now() if status == NotificationDelivery.Status.SENT else None,
    )
    if updated != 1:
        raise ProviderOutcomeUnknown("provider claim changed before its result was recorded")


def _complete_provider_delivery(claim, response: dict) -> None:
    from apps.notifications.models import NotificationDelivery

    _finish_provider_delivery(claim, NotificationDelivery.Status.SENT, response)


def _mark_provider_unknown(claim, exc: BaseException) -> None:
    from apps.notifications.models import NotificationDelivery

    evidence = dict(claim.provider_response or {})
    evidence.update({"error": type(exc).__name__, "reconciliation_required": True})
    NotificationDelivery.objects.filter(
        pk=claim.pk,
        status=NotificationDelivery.Status.CLAIMED,
    ).update(status=NotificationDelivery.Status.UNKNOWN, provider_response=evidence)


# ---------------------------------------------------------------------------
# Cohort announcements (D3-C-10) — chunked, rate-limited
# ---------------------------------------------------------------------------
@app.task(rate_limit="25/s")
def announce_cohort_chunk(
    *,
    announcement_id: str,
    title: str,
    body: str,
    context: dict,
    recipients: list[dict] | None = None,
    user_ids: list[int] | None = None,
) -> int:
    """Dispatch one announcement chunk to exact student principals.

    ``user_ids`` is accepted only to drain pre-deployment queued jobs. Those
    recipients go through conservative inference and are quarantined whenever
    a role-native owner cannot be proven.
    """
    from apps.notifications.models import EventType
    from apps.notifications.services import dispatch

    sent = 0
    principal_rows = list(recipients or [])
    principal_rows.extend({"user_id": uid} for uid in (user_ids or []))
    for recipient in principal_rows:
        uid = recipient["user_id"]
        result = dispatch(
            event_type=EventType.COHORTS_ANNOUNCEMENT,
            recipient_id=uid,
            context={"title": title, "body": body, **context},
            dedupe_key=f"cohorts.announcement:{announcement_id}:{uid}",
            recipient_principal_kind=recipient.get("principal_kind"),
            recipient_principal_id=recipient.get("principal_id"),
        )
        if result is not None and result.is_deliverable:
            sent += 1
    return sent


@app.task(rate_limit="25/s")
def dispatch_many_chunk(
    *,
    event_type: str,
    context: dict,
    dedupe_prefix: str | None = None,
    recipients: list[dict] | None = None,
    user_ids: list[int] | None = None,
) -> int:
    """Dispatch one event to a chunk of recipients off the request thread (D3-C).

    The offloaded form of receivers._dispatch_many for LARGE cohort fan-outs
    (lesson reschedule/cancel, assignment publish): looping dispatch() inline in the
    triggering HTTP request costs O(recipients) x ~3-4 queries each, saturating a DB
    connection and timing out a bulk reschedule for a big cohort. Same dedupe contract
    as the inline path (`{dedupe_prefix}:{uid}`)."""
    from apps.notifications.services import dispatch

    if (
        not isinstance(dedupe_prefix, str)
        or not dedupe_prefix
        or len(dedupe_prefix) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in dedupe_prefix)
    ):
        # This task is globally late-acknowledged. A chunk without a stable
        # domain discriminator could create and deliver every notification again
        # after worker loss, so the asynchronous path must never accept one.
        raise ValueError("A bounded dedupe_prefix is required for asynchronous notification fan-out.")

    sent = 0
    principal_rows = list(recipients or [])
    principal_rows.extend({"user_id": uid} for uid in (user_ids or []))
    for recipient in principal_rows:
        uid = recipient["user_id"]
        dedupe_key = f"{dedupe_prefix}:{uid}"
        notification = dispatch(
            event_type=event_type,
            recipient_id=uid,
            context=context,
            dedupe_key=dedupe_key,
            recipient_principal_kind=recipient.get("principal_kind"),
            recipient_principal_id=recipient.get("principal_id"),
        )
        if notification is not None and notification.is_deliverable:
            sent += 1
    return sent


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _offset_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValueError(f"{field} must be a bounded ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a bounded ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an offset")
    return value


def _move_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("move_id must be a 32-character lowercase hexadecimal identifier")
    return value


def _normalize_reschedule_moves(
    moves: object,
    *,
    maximum: int = LESSON_RESCHEDULE_MAX_EVENTS,
) -> list[RescheduleMove]:
    if not isinstance(moves, (list, tuple)) or not moves or len(moves) > maximum:
        raise ValueError(f"moves must contain between 1 and {maximum} events")
    normalized: list[RescheduleMove] = []
    seen: dict[tuple[int, str], RescheduleMove] = {}
    for raw in moves:
        if not isinstance(raw, dict) or set(raw) != {
            "lesson_id",
            "old_start",
            "moved_at",
            "move_id",
        }:
            raise ValueError("each move must contain lesson_id, old_start, moved_at, and move_id")
        move = RescheduleMove(
            lesson_id=_positive_int(raw["lesson_id"], field="lesson_id"),
            old_start=_offset_timestamp(raw["old_start"], field="old_start"),
            moved_at=_offset_timestamp(raw["moved_at"], field="moved_at"),
            move_id=_move_id(raw["move_id"]),
        )
        key = (move["lesson_id"], move["move_id"])
        prior = seen.get(key)
        if prior is not None:
            if prior != move:
                raise ValueError("one move identifier cannot describe two move snapshots")
            continue
        seen[key] = move
        normalized.append(move)
    return normalized


def _normalize_student_recipients(recipients: object) -> list[StudentRecipient]:
    if (
        not isinstance(recipients, (list, tuple))
        or not recipients
        or len(recipients) > LESSON_RESCHEDULE_RECIPIENTS_PER_TASK
    ):
        raise ValueError("recipients must be a non-empty bounded exact-principal chunk")
    normalized: list[StudentRecipient] = []
    seen: set[tuple[int, int]] = set()
    for raw in recipients:
        if not isinstance(raw, dict) or set(raw) != {
            "user_id",
            "principal_kind",
            "principal_id",
        }:
            raise ValueError("each recipient must be an exact principal descriptor")
        if raw["principal_kind"] != "student":
            raise ValueError("lesson reschedules can target student principals only")
        recipient = StudentRecipient(
            user_id=_positive_int(raw["user_id"], field="user_id"),
            principal_kind="student",
            principal_id=_positive_int(raw["principal_id"], field="principal_id"),
        )
        key = (recipient["user_id"], recipient["principal_id"])
        if key not in seen:
            seen.add(key)
            normalized.append(recipient)
    return normalized


@app.task(
    bind=True,
    max_retries=5,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
    rate_limit="10/s",
)
def coordinate_lesson_reschedule_fanout(
    self,
    *,
    cohort_id: int,
    moves: list[dict],
) -> dict[str, int]:
    """Expand one bulk-reschedule event entirely off the HTTP request path.

    Active cohort members are streamed once as exact student principals.  Child
    jobs are bounded in both dimensions and are safe to duplicate: their durable
    notification keys include exact principal + lesson + stable per-operation ID.
    """
    cohort_id = _positive_int(cohort_id, field="cohort_id")
    normalized_moves = _normalize_reschedule_moves(moves)

    from apps.cohorts.models import CohortMembership
    from core.utils import current_schema

    move_chunks = [
        normalized_moves[index : index + LESSON_RESCHEDULE_EVENTS_PER_TASK]
        for index in range(0, len(normalized_moves), LESSON_RESCHEDULE_EVENTS_PER_TASK)
    ]
    rows = (
        CohortMembership.objects.filter(
            cohort_id=cohort_id,
            end_date__isnull=True,
            student__is_active=True,
            student__user__is_active=True,
        )
        .order_by("student_id")
        .values_list("student__user_id", "student_id")
        .iterator(chunk_size=LESSON_RESCHEDULE_RECIPIENTS_PER_TASK)
    )
    schema = current_schema()
    recipient_chunk: list[StudentRecipient] = []
    recipients = 0
    queued = 0

    def enqueue_chunk() -> None:
        nonlocal queued
        for move_chunk in move_chunks:
            dispatch_lesson_reschedule_chunk.delay(
                moves=move_chunk,
                recipients=recipient_chunk.copy(),
                _schema_name=schema,
            )
            queued += 1

    try:
        for user_id, student_id in rows:
            recipient_chunk.append(
                StudentRecipient(
                    user_id=user_id,
                    principal_kind="student",
                    principal_id=student_id,
                )
            )
            recipients += 1
            if len(recipient_chunk) == LESSON_RESCHEDULE_RECIPIENTS_PER_TASK:
                enqueue_chunk()
                recipient_chunk.clear()
        if recipient_chunk:
            enqueue_chunk()
    except Exception as exc:
        # A retry may duplicate child messages that were already accepted.  The
        # child's principal-scoped notification dedupe makes that harmless.
        raise self.retry(exc=exc) from exc
    return {"events": len(normalized_moves), "recipients": recipients, "tasks": queued}


@app.task(
    bind=True,
    max_retries=5,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
    rate_limit="25/s",
)
def dispatch_lesson_reschedule_chunk(
    self,
    *,
    moves: list[dict],
    recipients: list[dict],
) -> dict[str, int]:
    """Create idempotent per-recipient notifications for one bounded work unit."""
    normalized_moves = _normalize_reschedule_moves(
        moves,
        maximum=LESSON_RESCHEDULE_EVENTS_PER_TASK,
    )
    normalized_recipients = _normalize_student_recipients(recipients)

    from apps.notifications.models import EventType
    from apps.notifications.services import dispatch

    attempted = 0
    deliverable = 0
    try:
        for move in normalized_moves:
            lesson_id = move["lesson_id"]
            move_id = move["move_id"]
            for recipient in normalized_recipients:
                attempted += 1
                notification = dispatch(
                    event_type=EventType.SCHEDULE_LESSON_REMINDER,
                    recipient_id=recipient["user_id"],
                    recipient_principal_kind=recipient["principal_kind"],
                    recipient_principal_id=recipient["principal_id"],
                    context={
                        "lesson_id": lesson_id,
                        "kind": "rescheduled",
                        "old_start": move["old_start"],
                    },
                    dedupe_key=(f"schedule.lesson_rescheduled:{lesson_id}:{move_id}:{recipient['user_id']}"),
                )
                if notification is not None and notification.is_deliverable:
                    deliverable += 1
    except Exception as exc:
        # Partial retries are safe because dispatch() get_or_create is scoped to
        # the immutable role principal and the per-move discriminator.
        raise self.retry(exc=exc) from exc
    return {"attempted": attempted, "deliverable": deliverable}
