"""Staff meetings (F3-5 / D-9).

A manager schedules a meeting and invites staff; each invitee RSVPs, and a teacher's
next meeting surfaces on their dashboard. Paper-elimination DNA — the meeting, its
agenda, and who accepted live in one place instead of a WhatsApp thread nobody can
audit later.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class StaffMeeting(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", _("Scheduled")
        CANCELLED = "cancelled", _("Cancelled")

    class ActorAttributionStatus(models.TextChoices):
        CAPTURED = "captured", _("Captured")
        RESOLVED = "resolved", _("Resolved from legacy data")
        QUARANTINED = "quarantined", _("Quarantined")
        NOT_APPLICABLE = "not_applicable", _("Not applicable")

    title = models.CharField(max_length=200)
    agenda = models.TextField(blank=True)
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="staff_meetings"
    )
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField()
    location = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    created_by_principal_kind = models.CharField(max_length=16, blank=True)
    created_by_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_by_attribution_status = models.CharField(
        max_length=16,
        choices=ActorAttributionStatus.choices,
        default=ActorAttributionStatus.QUARANTINED,
    )
    cancelled_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cancelled_by_principal_kind = models.CharField(max_length=16, blank=True)
    cancelled_by_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    cancelled_by_attribution_status = models.CharField(
        max_length=16,
        choices=ActorAttributionStatus.choices,
        default=ActorAttributionStatus.NOT_APPLICABLE,
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-starts_at",)
        indexes = [
            models.Index(fields=("branch", "status", "starts_at")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="meeting_ends_after_start"
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(created_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(created_by_principal_id__isnull=False)
                        & models.Q(created_by_attribution_status__in=("captured", "resolved"))
                    )
                    | models.Q(
                        created_by_principal_kind="",
                        created_by_principal_id__isnull=True,
                        created_by_attribution_status="quarantined",
                    )
                ),
                name="meeting_creator_principal_pair",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(cancelled_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(cancelled_by_principal_id__isnull=False)
                        & models.Q(cancelled_by_attribution_status__in=("captured", "resolved"))
                        & models.Q(status="cancelled")
                        & models.Q(cancelled_at__isnull=False)
                    )
                    | models.Q(
                        cancelled_by_principal_kind="",
                        cancelled_by_principal_id__isnull=True,
                        cancelled_by_attribution_status="not_applicable",
                        cancelled_by__isnull=True,
                        cancelled_at__isnull=True,
                        status="scheduled",
                    )
                    | models.Q(
                        cancelled_by_principal_kind="",
                        cancelled_by_principal_id__isnull=True,
                        cancelled_by_attribution_status="quarantined",
                    )
                ),
                name="meeting_canceller_principal_pair",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"meeting#{self.pk}:{self.title}:{self.status}"


class MeetingAttendee(models.Model):
    class Response(models.TextChoices):
        INVITED = "invited", _("Invited")
        ACCEPTED = "accepted", _("Accepted")
        DECLINED = "declined", _("Declined")

    meeting = models.ForeignKey(StaffMeeting, on_delete=models.CASCADE, related_name="attendees")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="meeting_invitations")
    principal_kind = models.CharField(max_length=16, blank=True)
    principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    response = models.CharField(max_length=8, choices=Response.choices, default=Response.INVITED)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=("meeting", "user"), name="one_invite_per_meeting_user"),
            models.UniqueConstraint(
                fields=("meeting", "principal_kind", "principal_id"),
                condition=models.Q(principal_id__isnull=False),
                name="one_invite_per_meeting_principal",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(principal_kind="", principal_id__isnull=True)
                    | (
                        models.Q(principal_kind__in=("staff", "teacher"))
                        & models.Q(principal_id__isnull=False)
                    )
                ),
                name="meeting_attendee_principal_pair",
            ),
        ]
        indexes = [
            models.Index(
                fields=("principal_kind", "principal_id", "meeting"),
                name="meeting_attendee_principal_idx",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"invite#{self.pk}:m{self.meeting_id}:u{self.user_id}:{self.response}"
