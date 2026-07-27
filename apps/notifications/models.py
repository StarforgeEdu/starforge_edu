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


class Channel(models.TextChoices):
    SMS = "sms", _("SMS")
    EMAIL = "email", _("Email")
    PUSH = "push", _("Push")
    IN_APP = "in_app", _("In-app")


class Locale(models.TextChoices):
    UZBEK = "uz", _("Uzbek")
    RUSSIAN = "ru", _("Russian")
    ENGLISH = "en", _("English")


class Notification(models.Model):
    """One event delivered to one user. Idempotent on ``dedupe_key``."""

    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="notifications", db_index=True
    )
    event_type = models.CharField(max_length=64, choices=EventType.choices, db_index=True)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    # Null = no idempotency requested (always a new row). Set = get_or_create
    # collapses repeat dispatches into the single existing row.
    dedupe_key = models.CharField(max_length=128, unique=True, null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("user", "read_at")),
            models.Index(fields=("user", "-created_at")),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"notif#{self.pk}:{self.event_type}->{self.user_id}"


class NotificationDelivery(models.Model):
    """One per-channel delivery attempt outcome for a Notification."""

    class Status(models.TextChoices):
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED_PREF = "skipped_pref", _("Skipped (preference off)")
        SKIPPED_QUIET_HOURS = "skipped_quiet_hours", _("Skipped (quiet hours)")
        DEAD_TOKEN = "dead_token", _("Dead push token")

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries", db_index=True
    )
    channel = models.CharField(max_length=16, choices=Channel.choices, db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, db_index=True)
    provider_response = models.JSONField(default=dict, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("notification", "channel")),
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
    event_type = models.CharField(max_length=64, choices=EventType.choices)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("user", "event_type", "channel")
        constraints = [
            models.UniqueConstraint(
                fields=("user", "event_type", "channel"),
                name="notif_pref_unique_user_event_channel",
            ),
        ]
        indexes = [models.Index(fields=("user", "event_type"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"pref#{self.user_id}:{self.event_type}:{self.channel}={self.enabled}"


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
