"""SMS campaigns (F10-1) — send one message to a student SEGMENT, reusing the Eskiz
client and recording every recipient.

A `Campaign` is built against a segment (branch + optional status/cohort filter); at
build time every matching student is frozen into a `CampaignRecipient` with the phone
the message will go to (the primary guardian's, else the student's own). Sending walks
the pending recipients once and stamps each as sent/failed — so the campaign and its
recipients ARE the audit trail of who was contacted, with what, and whether it landed
(the accountability/paper-elimination DNA: no untracked blasts).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Campaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENDING = "sending", _("Sending")  # claimed for a send pass (no re-send)
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")

    name = models.CharField(max_length=200)
    message = models.TextField()
    # The audience filter: {status?, cohort?}; the branch is the campaign's own branch.
    segment = models.JSONField(default=dict, blank=True)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="campaigns"
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT, db_index=True)
    # F10-1: an optional future send time. When set, the campaign stays DRAFT and a beat
    # task auto-sends it once `scheduled_at <= now` (a null means manual send only). Indexed
    # because the dispatcher scans `status=DRAFT, scheduled_at__lte=now` every cycle.
    scheduled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    total = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)  # no resolvable phone
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    sent_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("branch", "status")),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"campaign#{self.pk}:{self.name}:{self.status}"


class CampaignRecipient(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")  # no phone to send to

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="recipients")
    student = models.ForeignKey("students.StudentProfile", on_delete=models.PROTECT, related_name="+")
    phone = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("campaign", "student"), name="one_recipient_per_campaign_student"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"recipient#{self.pk}:c{self.campaign_id}:s{self.student_id}:{self.status}"


class DoNotContact(models.Model):
    """A phone number that has opted OUT of SMS campaigns (a do-not-contact list).

    Consent is keyed by PHONE — the unit SMS is actually sent to — so a guardian who
    asks to stop being texted is suppressed across ALL their children and every branch,
    not just one student. A campaign build SKIPS any recipient whose resolved phone is
    here, and send() re-checks it so an opt-out recorded between build and send is still
    honoured (dignity / anti-spam DNA: never text someone who said stop)."""

    phone = models.CharField(max_length=32, unique=True, db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"do_not_contact:{self.phone}"


class MessageTemplate(models.Model):
    """A reusable message template (F10-2). A staff member names a template + a short
    `purpose` brief, optionally has the AI draft its `body` (low-cost), edits it, and then
    reuses it when composing a campaign — so common messages (reminders, announcements,
    payment nudges) aren't retyped each time. `is_active` toggles it in/out of the picker
    without deleting the history."""

    name = models.CharField(max_length=120)
    category = models.CharField(max_length=40, blank=True)  # free label: reminder / payment / ...
    purpose = models.CharField(max_length=500, blank=True)  # the author's brief for AI drafting
    body = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name
