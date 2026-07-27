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
    cancelled_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
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
    response = models.CharField(max_length=8, choices=Response.choices, default=Response.INVITED)
    responded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(fields=("meeting", "user"), name="one_invite_per_meeting_user"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"invite#{self.pk}:m{self.meeting_id}:u{self.user_id}:{self.response}"
