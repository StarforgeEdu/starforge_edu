"""Notifications write-side services (D3-C-3/7/8/10).

The single fan-out for SMS / email / push / in-app / WebSocket (TD-15). Domain
apps never call adapters — they emit signals; ``apps/notifications/receivers``
calls ``dispatch()``; the Celery task does the per-channel routing.

Public contract (published to WORKLOG — Lanes A/B/E call/trigger these):

    dispatch(*, event_type, recipient_id, context, dedupe_key=None, channels=None,
             recipient_principal_kind=None, recipient_principal_id=None)
        -> Notification

    - get_or_create on ``dedupe_key`` => second call with the same key is a no-op
      that returns the existing row (and does NOT re-queue the task).
    - Queues ``dispatch_notification`` (Celery) on commit.
    - Unknown ``recipient_id`` is logged and dropped (raises nothing).
    - ``channels`` is an optional whitelist subset; None = all channels (subject
      to preferences).

DEFAULT_MATRIX — the per-(event_type, channel) opt-in default when a user has no
explicit ``NotificationPreference`` row:
    - in-app: ALWAYS on (every event).
    - SMS:   on for attendance.absent, payments.*, finance.*.
    - push:  on for everything.
    - email: on for finance.* and billing.*.

Quiet hours (from CenterSettings, default 22:00-07:00 Asia/Tashkent): SMS + push
are deferred via Celery ``eta`` to the window end; in-app + email send
immediately.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, time, timedelta
from string import Template
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.notifications.models import (
    DELIVERABLE_ATTRIBUTION_STATUSES,
    Channel,
    EventType,
    Notification,
    NotificationPreference,
    NotificationTemplate,
    RecipientAttributionStatus,
)
from core.utils import current_schema, stable_hash

logger = logging.getLogger("starforge.notifications")

ALL_CHANNELS = (Channel.IN_APP, Channel.EMAIL, Channel.SMS, Channel.PUSH)

# Events whose SMS channel defaults ON. push defaults ON everywhere; in-app
# always ON; email defaults ON for finance.* + billing.*.
_SMS_DEFAULT_ON = {
    EventType.ATTENDANCE_ABSENT,
    EventType.PAYMENTS_PAYMENT_COMPLETED,
    EventType.PAYMENTS_PAYMENT_FAILED,
    EventType.FINANCE_INVOICE_ISSUED,
    EventType.FINANCE_PAYMENT_REMINDER,
}
_EMAIL_DEFAULT_ON_PREFIXES = ("finance.", "billing.")

_OPERATOR_CHANNEL_FLAGS: dict[str, str] = {
    Channel.SMS: "SMS_ENABLED",
    Channel.EMAIL: "EMAIL_ENABLED",
    Channel.PUSH: "PUSH_NOTIFICATIONS_ENABLED",
}

_MAX_CONTEXT_ITEMS = 32
_MAX_CONTEXT_KEY_BYTES = 64
_MAX_CONTEXT_VALUE_BYTES = 1024
_MAX_TITLE_BYTES = 1024
_MAX_BODY_BYTES = 16 * 1024


def operator_channel_enabled(channel: str) -> bool:
    """Whether operations permit this outbound notification channel.

    In-app delivery is intentionally always available here: it is the durable
    feed and realtime websocket path, not an external provider.
    """
    flag = _OPERATOR_CHANNEL_FLAGS.get(channel)
    return True if flag is None else bool(getattr(settings, flag, True))


def default_channel_enabled(event_type: str, channel: str) -> bool:
    """The default matrix value for an (event_type, channel) with no pref row."""
    if channel == Channel.IN_APP:
        return True
    if channel == Channel.PUSH:
        return True
    if channel == Channel.SMS:
        return event_type in _SMS_DEFAULT_ON
    if channel == Channel.EMAIL:
        return event_type.startswith(_EMAIL_DEFAULT_ON_PREFIXES)
    return False


def channel_enabled_for_user(
    *,
    user_id: int,
    recipient_principal_kind: str,
    recipient_principal_id: int,
    event_type: str,
    channel: str,
) -> bool:
    """Effective role-principal opt-in; an exact override wins over defaults."""
    pref = (
        NotificationPreference.objects.filter(
            user_id=user_id,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
            attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
            event_type=event_type,
            channel=channel,
        )
        .values_list("enabled", flat=True)
        .first()
    )
    if pref is not None:
        return pref
    return default_channel_enabled(event_type, channel)


# ---------------------------------------------------------------------------
# dispatch — the public entry point
# ---------------------------------------------------------------------------
@transaction.atomic
def dispatch(
    *,
    event_type: str,
    recipient_id: int,
    context: dict[str, Any],
    dedupe_key: str | None = None,
    channels: list[str] | None = None,
    recipient_principal_kind: str | None = None,
    recipient_principal_id: int | None = None,
) -> Notification | None:
    """Create (idempotently) a Notification for one recipient and queue fan-out.

    Returns the Notification (existing one on a dedupe hit), or None when the
    recipient does not exist (logged + dropped — raises nothing).
    """
    from apps.users.models import User  # lazy: avoid import cost at module load

    recipient_user = None
    if isinstance(recipient_id, int) and not isinstance(recipient_id, bool) and recipient_id > 0:
        recipient_user = User.objects.only("pk", "is_active").filter(pk=recipient_id).first()
    if recipient_user is None:
        logger.warning(
            "dispatch dropped: unknown user id=%s event=%s schema=%s",
            recipient_id,
            event_type,
            current_schema(),
        )
        return None
    if event_type not in EventType.values:
        raise ValueError("event_type must be a canonical notification event")
    if channels is not None:
        if not isinstance(channels, (list, tuple)):
            raise TypeError("channels must be a sequence or None")
        invalid_channels = set(channels) - set(ALL_CHANNELS)
        if invalid_channels:
            raise ValueError("channels contains an unsupported notification channel")
    if dedupe_key is not None and not isinstance(dedupe_key, str):
        raise TypeError("dedupe_key must be a string or None")
    if dedupe_key and (len(dedupe_key) > 128 or "\x00" in dedupe_key):
        # Preserve idempotency without letting an oversized or NUL-containing
        # producer key fail as a database error inside an unrelated domain write.
        dedupe_key = f"bounded:v1:{stable_hash(dedupe_key)}"

    from apps.notifications.principals import resolve_recipient_principal

    principal = resolve_recipient_principal(
        user_id=recipient_id,
        principal_kind=recipient_principal_kind,
        principal_id=recipient_principal_id,
        user_is_active=recipient_user.is_active,
    )
    safe_context = _json_safe(context)
    title, body = render_template(
        event_type=event_type,
        channel=Channel.IN_APP,
        user_id=recipient_id,
        context=safe_context,
    )
    title = _bounded_utf8(title, _MAX_TITLE_BYTES)[:255]
    body = _bounded_utf8(body, _MAX_BODY_BYTES)

    snapshot = {
        "recipient_principal_kind": principal.kind,
        "recipient_principal_id": principal.principal_id,
        "attribution_status": principal.status,
    }
    defaults = {
        "user_id": recipient_id,
        "event_type": event_type,
        "title": title,
        "body": body,
        "data": safe_context,
        **snapshot,
    }
    if dedupe_key:
        identity_lookup = (
            {
                "user_id": recipient_id,
                "recipient_principal_kind": principal.kind,
                "recipient_principal_id": principal.principal_id,
            }
            if principal.is_deliverable
            else {
                "user_id": recipient_id,
                "recipient_principal_kind": None,
                "recipient_principal_id": None,
            }
        )
        notification, created = Notification.objects.get_or_create(
            dedupe_key=dedupe_key,
            **identity_lookup,
            defaults=defaults,
        )
        if not created:
            # Idempotent no-op: do NOT re-queue the fan-out task.
            return notification
    else:
        notification = Notification.objects.create(**defaults)

    if not notification.is_deliverable:
        logger.warning(
            "notification quarantined: user=%s event=%s reason=%s schema=%s",
            recipient_id,
            event_type,
            principal.reason,
            current_schema(),
        )
        return notification

    schema = current_schema()
    notif_id = notification.pk
    # ``None`` means the default channel matrix; an explicit empty whitelist
    # means no fan-out. Never broaden [] into all channels.
    chans = None if channels is None else list(channels)
    transaction.on_commit(lambda: _queue_dispatch(notif_id, chans, schema))
    return notification


def _queue_dispatch(notification_id: int, channels: list[str] | None, schema: str) -> None:
    from celery_tasks.notification_tasks import dispatch_notification

    dispatch_notification.delay(notification_id, channels=channels, _schema_name=schema)


# ---------------------------------------------------------------------------
# Realtime producers (D4-LC-6) — the ONLY place dispatch's fan-out talks to the
# Channels layer (TD-15: dispatch is the single group_send producer). The
# producer-uniqueness grep test asserts `channel_layer.group_send` is imported
# only under apps/notifications/ + infrastructure/websocket/. The celery in-app
# delivery calls push_in_app() rather than importing group_send itself so this
# module stays the sole producer call site in the notifications stack.
#
# Group names are SCHEMA-PREFIXED: role-principal/cohort ids are per-tenant
# autoincrements, so an unscoped name collides across tenants on shared Redis.
# NotificationConsumer joins ``{schema}.n.{kind}.{id}``; AttendanceConsumer
# joins ``{schema}.cohort.{id}``.
# ---------------------------------------------------------------------------
def push_in_app(notification, title: str, body: str) -> None:
    """Send to the exact tenant + role-native recipient group.

    Payload ``type`` ``notification.message`` routes to
    ``NotificationConsumer.notification_message`` (Channels maps dots to
    underscores). Called from ``dispatch_notification`` (the in-app channel).
    """
    from infrastructure.websocket.channel_layer import group_send
    from infrastructure.websocket.groups import notification_principal_group

    group_send(
        notification_principal_group(
            current_schema(),
            notification.recipient_principal_kind,
            notification.recipient_principal_id,
        ),
        {
            "type": "notification.message",
            "recipient_principal_kind": notification.recipient_principal_kind,
            "recipient_principal_id": notification.recipient_principal_id,
            "id": notification.pk,
            "event_type": notification.event_type,
            "title": title,
            "body": body,
            "data": dict(notification.data or {}),
            "created_at": notification.created_at.isoformat(),
        },
    )


def push_cohort_attendance(*, cohort_id: int, payload: dict[str, Any]) -> None:
    """group_send a live attendance update to ``{schema}.cohort.{cohort_id}``.

    Payload ``type`` ``attendance.update`` routes to
    ``AttendanceConsumer.attendance_update``. Producer of record for the cohort
    attendance channel (TD-15) — called from the attendance notification
    receiver (once per attendance event), never from apps.attendance directly.
    """
    from infrastructure.websocket.channel_layer import group_send
    from infrastructure.websocket.groups import cohort_attendance_group

    group_send(
        cohort_attendance_group(current_schema(), cohort_id),
        {"type": "attendance.update", **payload},
    )


def _json_safe(context: dict[str, Any]) -> dict[str, Any]:
    """Return a small JSON-safe context for storage and provider fan-out.

    Domain payloads can contain model/date/Decimal objects and occasionally
    user-controlled text. Bounding both cardinality and UTF-8 size prevents an
    accidental context from becoming an oversized database row, Redis message,
    SMS, or push payload. Truncation affects notification presentation only; it
    never mutates the authoritative domain record referenced by the context.
    """

    if not isinstance(context, dict):
        raise TypeError("notification context must be an object")
    safe: dict[str, Any] = {}
    for index, (key, value) in enumerate(context.items()):
        if index >= _MAX_CONTEXT_ITEMS:
            break
        if not isinstance(key, str) or not key or "\x00" in key:
            # Provider data maps require string keys. A malformed internal key
            # is a programmer error, not something to coerce into a collision.
            raise ValueError("notification context keys must be non-empty strings without NUL")
        bounded_key = _bounded_utf8(key, _MAX_CONTEXT_KEY_BYTES)
        if bounded_key in safe:
            raise ValueError("notification context keys collide after size normalization")
        if isinstance(value, str):
            safe[bounded_key] = _bounded_utf8(value.replace("\x00", "�"), _MAX_CONTEXT_VALUE_BYTES)
        elif isinstance(value, float) and not math.isfinite(value):
            safe[bounded_key] = str(value)
        elif isinstance(value, (int, float, bool, type(None))):
            safe[bounded_key] = value
        else:
            safe[bounded_key] = _bounded_utf8(
                str(value).replace("\x00", "�"),
                _MAX_CONTEXT_VALUE_BYTES,
            )
    return safe


def _bounded_utf8(value: object, max_bytes: int) -> str:
    text = str(value)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Template rendering (D3-C-7)
# ---------------------------------------------------------------------------
def _user_locale(user_id: int) -> str:
    from apps.org.selectors import get_center_settings
    from apps.users.models import User

    lang = User.objects.filter(pk=user_id).values_list("preferred_language", flat=True).first()
    if lang:
        return lang
    # Fall back to the center default grading/locale knob if one exists, else uz.
    settings_obj = get_center_settings()
    return getattr(settings_obj, "default_language", "") or "uz"


def render_template(
    *, event_type: str, channel: str, user_id: int, context: dict[str, Any]
) -> tuple[str, str]:
    """Return ``(subject, body)`` for an (event_type, channel, user-locale).

    Locale resolution: ``User.preferred_language`` -> en->uz fallback chain.
    Rendering: ``string.Template.safe_substitute`` — missing vars render
    literally, NO attribute access, NO eval (Jinja-safe per TASKS §17).
    """
    locale = _user_locale(user_id)
    template = _lookup_template(event_type=event_type, channel=channel, locale=locale)
    if template is None:
        # No template at all for this event/channel: degrade to a generic line so
        # an in-app row still carries something readable.
        label = dict(EventType.choices).get(event_type, event_type)
        return str(label), ""
    subject = Template(template.subject).safe_substitute(context) if template.subject else ""
    body = Template(template.body).safe_substitute(context)
    return subject, body


def _center_default_locale() -> str:
    """The center's *explicitly configured* default notification language
    (CenterSettings.default_language). Returns "" when unset so the implicit
    platform default does NOT leapfrog the en lingua-franca step — uz is still
    the final fallback in `_fallback_locales`. A center that sets default_language
    (e.g. "uz") gets that variant preferred over en."""
    from apps.org.selectors import get_center_settings

    try:
        settings_obj = get_center_settings()
    except Exception:  # public schema / no settings row — no configured default
        return ""
    return getattr(settings_obj, "default_language", "") or ""


# Locale fallback order (D4-LF-3): requested -> center-default -> en -> uz.
def _fallback_locales(locale: str) -> list[str]:
    chain = [locale]
    for fallback in (_center_default_locale(), "en", "uz"):
        if fallback and fallback not in chain:
            chain.append(fallback)
    return chain


def _lookup_template(*, event_type: str, channel: str, locale: str) -> NotificationTemplate | None:
    rows = list(NotificationTemplate.objects.filter(event_type=event_type, channel=channel, is_active=True))
    by_locale = {row.locale: row for row in rows}
    for candidate in _fallback_locales(locale):
        if candidate in by_locale:
            # D4-LF-3: the user's preferred_language variant should exist; when it
            # doesn't we serve a fallback (center-default -> en -> uz) but log a
            # warning so the gap is observable (the completeness test asserts every
            # event type has uz+en+ru in_app rows).
            if candidate != locale:
                logger.warning(
                    "notification template fallback: event=%s channel=%s wanted=%s served=%s schema=%s",
                    event_type,
                    channel,
                    locale,
                    candidate,
                    current_schema(),
                )
            return by_locale[candidate]
    return None


# ---------------------------------------------------------------------------
# Quiet hours (D3-C-8)
# ---------------------------------------------------------------------------
def in_quiet_hours(*, at: datetime, start: time, end: time) -> bool:
    """True if ``at`` (tz-aware) falls inside the [start, end) quiet window.

    Handles wrap-around windows (e.g. 22:00-07:00 spans midnight).
    """
    now_t = timezone.localtime(at).time()
    if start <= end:
        return start <= now_t < end
    # Wrap-around: inside if at/after start OR before end.
    return now_t >= start or now_t < end


def quiet_hours_eta(*, at: datetime, end: time) -> datetime:
    """The datetime at which the quiet window ends, on or after ``at``."""
    local = timezone.localtime(at)
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = candidate + timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# Preferences bulk upsert (D3-C-8 / endpoint)
# ---------------------------------------------------------------------------
@transaction.atomic
def upsert_preferences(
    *,
    user,
    recipient_principal_kind: str,
    recipient_principal_id: int,
    rows: list[dict[str, Any]],
) -> list[NotificationPreference]:
    """Bulk upsert overrides for one exact role-native recipient."""
    out: list[NotificationPreference] = []
    for row in rows:
        pref, _created = NotificationPreference.objects.update_or_create(
            user=user,
            recipient_principal_kind=recipient_principal_kind,
            recipient_principal_id=recipient_principal_id,
            event_type=row["event_type"],
            channel=row["channel"],
            defaults={"enabled": row["enabled"]},
            create_defaults={
                "user": user,
                "enabled": row["enabled"],
                "attribution_status": RecipientAttributionStatus.CAPTURED,
            },
        )
        out.append(pref)
    return out


# ---------------------------------------------------------------------------
# Read receipts (D3-C-9)
# ---------------------------------------------------------------------------
@transaction.atomic
def mark_read(
    *,
    user,
    recipient_principal_kind: str,
    recipient_principal_id: int,
    notification_id: int,
) -> bool:
    """Mark one of the user's own notifications read. Returns True if a row changed."""
    updated = Notification.objects.filter(
        pk=notification_id,
        user=user,
        recipient_principal_kind=recipient_principal_kind,
        recipient_principal_id=recipient_principal_id,
        attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
        read_at__isnull=True,
    ).update(read_at=timezone.now())
    return bool(updated)


@transaction.atomic
def mark_all_read(*, user, recipient_principal_kind: str, recipient_principal_id: int) -> int:
    """Mark every unread notification of the user read in a single UPDATE."""
    return Notification.objects.filter(
        user=user,
        recipient_principal_kind=recipient_principal_kind,
        recipient_principal_id=recipient_principal_id,
        attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
        read_at__isnull=True,
    ).update(read_at=timezone.now())


# ---------------------------------------------------------------------------
# Cohort announcements (D3-C-10)
# ---------------------------------------------------------------------------
def announce_cohort(
    *, cohort_id: int, title: str, body: str, actor=None, announcement_id: str | None = None
) -> dict[str, Any]:
    """Fan out a ``cohorts.announcement`` to every active member, chunked + rate
    limited (the per-user task carries ``rate_limit="25/s"``).

    Dedupe key per (announcement, user) so a re-fire of the same announcement
    delivers each member exactly once.
    """
    from apps.cohorts.models import Cohort, CohortMembership

    if not Cohort.objects.filter(pk=cohort_id).exists():
        from core.exceptions import ValidationException

        raise ValidationException(
            _("cohort does not exist."),
            code="validation_error",
            fields={"cohort": ["Object does not exist."]},
        )

    ann_id = announcement_id or stable_hash(f"{cohort_id}:{title}:{timezone.now().isoformat()}")[:24]
    recipients = list(
        CohortMembership.objects.filter(cohort_id=cohort_id, end_date__isnull=True).values_list(
            "student__user_id", "student_id"
        )
    )
    schema = current_schema()
    context = {"title": title, "body": body}

    from celery_tasks.notification_tasks import announce_cohort_chunk

    chunk_size = 100
    chunks = 0
    for start in range(0, len(recipients), chunk_size):
        batch = recipients[start : start + chunk_size]
        announce_cohort_chunk.delay(
            recipients=[
                {"user_id": user_id, "principal_kind": "student", "principal_id": student_id}
                for user_id, student_id in batch
            ],
            announcement_id=ann_id,
            title=title,
            body=body,
            context=context,
            _schema_name=schema,
        )
        chunks += 1
    return {"announcement_id": ann_id, "recipients": len(recipients), "chunks": chunks}
