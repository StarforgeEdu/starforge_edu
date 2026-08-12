"""Notifications models (D3-C-1/2).

The central messaging substrate: a ``Notification`` is created once per event per
recipient (idempotent on ``dedupe_key``), fanned out across channels by the
Celery task, and each per-channel outcome recorded as a ``NotificationDelivery``
row. Preferences + templates are tenant-schema configuration.

EventType is the canonical list from DAY-3.md D3-C-2 — *extend, never rename*.
Each value is the ``"<domain>.<event>"`` form the source signal maps to (see
``apps/notifications/receivers.py`` for the signal->event-type mapping table).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.fields.json import KeyTransform
from django.utils.translation import gettext_lazy as _


class EventType(models.TextChoices):
    """Every signal emitted on Days 1-3 that becomes a notification.

    Verified against WORKLOG-published signal names (Days 1-2) plus today's
    lanes A/B/E. Where a source signal does not exist yet (students enrollment,
    academics grade-publication), the event type is still defined so dispatch()
    accepts it; the receiver connects once the owning lane emits the signal.
    """

    ATTENDANCE_ABSENT = "attendance.absent", _("Attendance: absent")
    ATTENDANCE_LATE = "attendance.late", _("Attendance: late")
    ACADEMICS_GRADES_PUBLISHED = "academics.grades_published", _("Academics: grades published")
    ASSIGNMENTS_CREATED = "assignments.created", _("Assignment created")
    ASSIGNMENTS_DUE_SOON = "assignments.due_soon", _("Assignment due soon")
    ASSIGNMENTS_GRADED = "assignments.graded", _("Assignment graded")
    SCHEDULE_LESSON_REMINDER = "schedule.lesson_reminder", _("Lesson reminder")
    SCHEDULE_CYCLE_EXAM_REMINDER = (
        "schedule.cycle_exam_reminder",
        _("Cycle exam reminder"),
    )
    AUTH_NEW_DEVICE_LOGIN = "auth.new_device_login", _("New device login")
    STUDENTS_ENROLLMENT_CHANGED = "students.enrollment_changed", _("Enrollment changed")
    FINANCE_INVOICE_ISSUED = "finance.invoice_issued", _("Invoice issued")
    FINANCE_PAYMENT_REMINDER = "finance.payment_reminder", _("Payment reminder")
    PAYMENTS_PAYMENT_COMPLETED = "payments.payment_completed", _("Payment completed")
    PAYMENTS_PAYMENT_FAILED = "payments.payment_failed", _("Payment failed")
    COHORTS_ANNOUNCEMENT = "cohorts.announcement", _("Cohort announcement")
    BILLING_SUBSCRIPTION_PAST_DUE = "billing.subscription_past_due", _("Subscription past due")
    BILLING_SUBSCRIPTION_SUSPENDED = "billing.subscription_suspended", _("Subscription suspended")
    PRINT_JOB_FAILED = "print.failed", _("Print job failed")  # D4-LD-4
    # A-1 Approvals engine
    APPROVAL_APPROVED = "approval.approved", _("Request approved")
    APPROVAL_REJECTED = "approval.rejected", _("Request rejected")
    APPROVAL_AWAITING_DISBURSEMENT = "approval.awaiting_disbursement", _("Approved — awaiting disbursement")
    APPROVAL_DISBURSED = "approval.disbursed", _("Request disbursed")
    # F24-1: a student crossed the center's penalty-point escalation threshold.
    PENALTY_ESCALATED = "penalty.escalated", _("Penalty: escalation threshold crossed")
    # Cross-domain events already emitted by their owning services. Keeping the
    # canonical choices here makes preferences and localized templates available
    # for every event that can reach dispatch().
    MESSAGE_RECEIVED = "message.received", _("Message received")
    REPORT_READY = "report.ready", _("Report ready")
    COVER_REQUESTED = "cover.requested", _("Cover requested")
    COVER_APPROVED = "cover.approved", _("Cover approved")
    COVER_POOL_OPENED = "cover.pool_opened", _("Cover pool opened")
    COVER_REJECTED = "cover.rejected", _("Cover rejected")


class Channel(models.TextChoices):
    SMS = "sms", _("SMS")
    EMAIL = "email", _("Email")
    PUSH = "push", _("Push")
    IN_APP = "in_app", _("In-app")


class Locale(models.TextChoices):
    UZBEK = "uz", _("Uzbek")
    RUSSIAN = "ru", _("Russian")
    ENGLISH = "en", _("English")


class RecipientPrincipalKind(models.TextChoices):
    """Role-native account that owns private notification state."""

    STUDENT = "student", _("Student")
    TEACHER = "teacher", _("Teacher")
    PARENT = "parent", _("Parent")
    STAFF = "staff", _("Staff")


class RecipientAttributionStatus(models.TextChoices):
    """Confidence/state of an immutable recipient snapshot.

    Only ``captured`` and ``resolved`` rows may be read or delivered.  Existing
    rows start ``unresolved`` during the additive migration and therefore fail
    closed until the reviewed backfill command can prove their owner.
    """

    CAPTURED = "captured", _("Captured at write time")
    RESOLVED = "resolved", _("Resolved by reviewed backfill")
    UNRESOLVED = "unresolved", _("Unresolved")
    CONFLICTING = "conflicting", _("Conflicting evidence")
    QUARANTINED = "quarantined", _("Quarantined for review")


DELIVERABLE_ATTRIBUTION_STATUSES = (
    RecipientAttributionStatus.CAPTURED,
    RecipientAttributionStatus.RESOLVED,
)


_RECIPIENT_SNAPSHOT_FIELDS = (
    "user_id",
    "recipient_principal_kind",
    "recipient_principal_id",
    "attribution_status",
)


def _guard_immutable_recipient_snapshot(instance, *, update_fields=None) -> None:
    """Reject ordinary attempts to rewrite a persisted recipient identity.

    The database trigger in migration 0012 is the authoritative guard (and also
    covers ``QuerySet.update``/raw SQL).  This model-level check gives ordinary
    callers a useful validation error before the query reaches PostgreSQL.
    """

    if instance._state.adding or instance.pk is None:
        return
    if update_fields is not None:
        updated = {str(field) for field in update_fields}
        if updated.isdisjoint(_RECIPIENT_SNAPSHOT_FIELDS):
            return
    previous = (
        type(instance)._default_manager.filter(pk=instance.pk).values(*_RECIPIENT_SNAPSHOT_FIELDS).first()
    )
    if previous is None:
        return
    changed = [field for field in _RECIPIENT_SNAPSHOT_FIELDS if previous[field] != getattr(instance, field)]
    if changed:
        raise ValidationError(
            {field: [str(_("Notification recipient attribution is immutable."))] for field in changed}
        )


def _capture_new_recipient_snapshot(instance) -> None:
    """Capture or quarantine the recipient for every ordinary ORM create.

    ``dispatch()`` supplies an explicit principal whenever the producer knows
    it, but admin/import/seed code can still create these models directly.  A
    direct write must not silently create bridge-user-owned private state.  The
    resolver accepts an explicit principal only when it belongs to the linked
    active user and otherwise infers only a single, unambiguous role profile.
    """

    if not instance._state.adding or instance.user_id is None:
        return
    if (
        instance.attribution_status in DELIVERABLE_ATTRIBUTION_STATUSES
        and instance.recipient_principal_kind in RecipientPrincipalKind.values
        and instance.recipient_principal_id is not None
    ):
        # Explicit producers resolve before construction. The database trigger
        # validates that exact snapshot against the active role row, avoiding a
        # duplicate user/profile lookup on every recipient in a large fan-out.
        return
    if (
        instance.recipient_principal_kind is None
        and instance.recipient_principal_id is None
        and instance.attribution_status
        in (
            RecipientAttributionStatus.CONFLICTING,
            RecipientAttributionStatus.QUARANTINED,
        )
    ):
        # A trusted writer already failed closed with a more precise outcome.
        # Re-inferring here could downgrade ``principal_not_owned`` quarantine
        # into a generic multi-profile conflict and destroy review evidence.
        return
    from apps.notifications.principals import resolve_recipient_principal

    principal = resolve_recipient_principal(
        user_id=instance.user_id,
        principal_kind=instance.recipient_principal_kind,
        principal_id=instance.recipient_principal_id,
    )
    instance.recipient_principal_kind = principal.kind
    instance.recipient_principal_id = principal.principal_id
    instance.attribution_status = principal.status


class Notification(models.Model):
    """One event delivered to one role-native recipient.

    ``user`` remains the compatibility/audit bridge.  Authorization and
    delivery always use the immutable role-native kind/id snapshot below.
    """

    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="notifications", db_index=True
    )
    event_type = models.CharField(max_length=64, choices=EventType.choices, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    recipient_principal_kind = models.CharField(
        max_length=16,
        choices=RecipientPrincipalKind.choices,
        null=True,
        blank=True,
        editable=False,
    )
    recipient_principal_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
    )
    attribution_status = models.CharField(
        max_length=12,
        choices=RecipientAttributionStatus.choices,
        default=RecipientAttributionStatus.UNRESOLVED,
        db_default=RecipientAttributionStatus.UNRESOLVED,
        editable=False,
    )
    # Idempotency is scoped to the immutable recipient. A shared bridge user may
    # legitimately receive the same domain event in two different roles.
    dedupe_key = models.CharField(max_length=128, null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "read_at")),
            models.Index(fields=("user", "-created_at")),
            models.Index(
                fields=(
                    "user",
                    "recipient_principal_kind",
                    "recipient_principal_id",
                    "read_at",
                ),
                name="notif_principal_unread_idx",
            ),
            models.Index(
                fields=(
                    "user",
                    "recipient_principal_kind",
                    "recipient_principal_id",
                    "-created_at",
                    "-id",
                ),
                name="notif_principal_time_idx",
            ),
            models.Index(
                fields=("attribution_status", "-created_at", "-id"),
                name="notif_attribution_queue_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
                        recipient_principal_kind__in=RecipientPrincipalKind.values,
                        recipient_principal_id__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=(
                            RecipientAttributionStatus.UNRESOLVED,
                            RecipientAttributionStatus.CONFLICTING,
                            RecipientAttributionStatus.QUARANTINED,
                        ),
                        recipient_principal_kind__isnull=True,
                        recipient_principal_id__isnull=True,
                    )
                ),
                name="notif_recipient_attribution_shape",
            ),
            models.UniqueConstraint(
                fields=("recipient_principal_kind", "recipient_principal_id", "dedupe_key"),
                condition=models.Q(
                    dedupe_key__isnull=False,
                    attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
                ),
                name="notif_principal_dedupe_unique",
            ),
            models.UniqueConstraint(
                fields=("user", "dedupe_key"),
                condition=models.Q(
                    dedupe_key__isnull=False,
                    attribution_status__in=(
                        RecipientAttributionStatus.UNRESOLVED,
                        RecipientAttributionStatus.CONFLICTING,
                        RecipientAttributionStatus.QUARANTINED,
                    ),
                ),
                name="notif_quarantine_dedupe_unique",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"notif#{self.pk}:{self.event_type}->{self.user_id}"

    def save(self, *args, **kwargs) -> None:
        _capture_new_recipient_snapshot(self)
        _guard_immutable_recipient_snapshot(self, update_fields=kwargs.get("update_fields"))
        super().save(*args, **kwargs)

    @property
    def is_deliverable(self) -> bool:
        return self.attribution_status in DELIVERABLE_ATTRIBUTION_STATUSES


class NotificationDelivery(models.Model):
    """One per-channel delivery attempt outcome for a Notification."""

    class Status(models.TextChoices):
        CLAIMED = "claimed", _("Claimed for provider delivery")
        UNKNOWN = "unknown", _("Provider outcome unknown")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED_PREF = "skipped_pref", _("Skipped (preference off)")
        SKIPPED_DISABLED = "skipped_disabled", _("Skipped (channel disabled by operator)")
        SKIPPED_QUIET_HOURS = "skipped_quiet_hours", _("Skipped (quiet hours)")
        DEAD_TOKEN = "dead_token", _("Dead push token")

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries", db_index=True
    )
    channel = models.CharField(max_length=16, choices=Channel.choices, db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, db_index=True)
    provider_response = models.JSONField(default=dict, blank=True)
    # Opaque logical destination identifier used only to serialize external
    # provider contact.  It deliberately contains no address/token material.
    delivery_key = models.CharField(max_length=160, null=True, blank=True, editable=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("notification", "channel")),
            models.Index(
                fields=("status", "created_at"),
                name="notif_delivery_status_created",
            ),
            # Push dead-token tracking looks up the newest three attempts for one
            # provider device id. The id lives in JSON, so the ordinary delivery
            # indexes cannot support that equality + newest-first query; without
            # this partial expression index, every failure scans/sorts a table that
            # grows forever.
            models.Index(
                KeyTransform("device_id", models.F("provider_response")),
                models.F("created_at").desc(),
                condition=models.Q(channel=Channel.PUSH),
                include=("notification", "status"),
                name="notif_push_device_created_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("notification", "channel", "delivery_key"),
                condition=models.Q(
                    delivery_key__isnull=False,
                    status__in=("claimed", "unknown", "sent"),
                ),
                name="notif_one_provider_contact_per_destination",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"delivery#{self.pk}:{self.channel}:{self.status}"


class NotificationPreference(models.Model):
    """A user's per-(event_type, channel) opt-in override.

    An ABSENT row means "use the default matrix" (see services.DEFAULT_MATRIX) —
    rows are only written when a user diverges from the default.
    """

    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="notification_preferences", db_index=True
    )
    recipient_principal_kind = models.CharField(
        max_length=16,
        choices=RecipientPrincipalKind.choices,
        null=True,
        blank=True,
        editable=False,
    )
    recipient_principal_id = models.PositiveBigIntegerField(
        null=True,
        blank=True,
        editable=False,
    )
    attribution_status = models.CharField(
        max_length=12,
        choices=RecipientAttributionStatus.choices,
        default=RecipientAttributionStatus.UNRESOLVED,
        db_default=RecipientAttributionStatus.UNRESOLVED,
        editable=False,
    )
    event_type = models.CharField(max_length=64, choices=EventType.choices)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user", "event_type", "channel")
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "recipient_principal_kind",
                    "recipient_principal_id",
                    "event_type",
                    "channel",
                ),
                condition=models.Q(attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES),
                name="notif_pref_principal_event_channel_unique",
            ),
            models.UniqueConstraint(
                fields=("user", "event_type", "channel"),
                condition=models.Q(
                    attribution_status__in=(
                        RecipientAttributionStatus.UNRESOLVED,
                        RecipientAttributionStatus.CONFLICTING,
                        RecipientAttributionStatus.QUARANTINED,
                    )
                ),
                name="notif_pref_quarantine_event_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=DELIVERABLE_ATTRIBUTION_STATUSES,
                        recipient_principal_kind__in=RecipientPrincipalKind.values,
                        recipient_principal_id__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=(
                            RecipientAttributionStatus.UNRESOLVED,
                            RecipientAttributionStatus.CONFLICTING,
                            RecipientAttributionStatus.QUARANTINED,
                        ),
                        recipient_principal_kind__isnull=True,
                        recipient_principal_id__isnull=True,
                    )
                ),
                name="notif_pref_attribution_shape",
            ),
        ]
        indexes = [
            models.Index(fields=("user", "event_type")),
            models.Index(
                fields=(
                    "user",
                    "recipient_principal_kind",
                    "recipient_principal_id",
                    "event_type",
                ),
                name="notif_pref_principal_event_idx",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"pref#{self.user_id}:{self.event_type}:{self.channel}={self.enabled}"

    def save(self, *args, **kwargs) -> None:
        _capture_new_recipient_snapshot(self)
        _guard_immutable_recipient_snapshot(self, update_fields=kwargs.get("update_fields"))
        super().save(*args, **kwargs)


class NotificationTemplate(models.Model):
    """Localized title/body template for an (event_type, channel, locale).

    Bodies use ``string.Template`` placeholders (``$student_name``) rendered via
    ``safe_substitute`` — no attribute access, no eval (Jinja-safe per TASKS §17).
    """

    event_type = models.CharField(max_length=64, choices=EventType.choices, db_index=True)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    locale = models.CharField(max_length=2, choices=Locale.choices)
    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("event_type", "channel", "locale")
        constraints = [
            models.UniqueConstraint(
                fields=("event_type", "channel", "locale"),
                name="notif_template_unique_event_channel_locale",
            ),
        ]
        indexes = [models.Index(fields=("event_type", "channel", "locale"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"tmpl:{self.event_type}:{self.channel}:{self.locale}"
