"""Rule book / policy acknowledgment (PRODUCT_VISION #12).

Each center has a rule book. Everyone must be FORCED to read and accept the rules
that apply to their role, and re-accept when a rule changes (version bump). Content
is role-filtered: a cashier shouldn't see teacher-only rules.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class Rule(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    # Bumped whenever the body changes -> everyone must re-acknowledge.
    version = models.PositiveIntegerField(default=1)
    # Role codes this rule applies to (e.g. ["teacher", "assistant"]); empty = everyone.
    applies_to_roles = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("title",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} v{self.version}"


class RuleAcknowledgment(models.Model):
    """One person's acceptance of one rule AT a specific version. A new version
    leaves the old ack in place but no longer counts as 'current', so the rule
    re-appears as pending."""

    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name="acknowledgments")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="rule_acknowledgments")
    version = models.PositiveIntegerField()
    acknowledged_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-acknowledged_at",)
        constraints = [
            models.UniqueConstraint(fields=("rule", "user", "version"), name="one_ack_per_rule_user_version"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"ack:{self.user_id}:{self.rule_id}v{self.version}"


class Penalty(models.Model):
    """A demerit issued for breaching a rule (F24-1). The subject is EITHER a student OR
    a staff member (exactly one — CheckConstraint), so the same accountable / correctable
    / transparent ledger covers student demerits and staff discipline. Accountable (records
    who issued it and why), correctable (a manager WAIVES it, with a reason), transparent
    (the subject sees their OWN record). `points` accrue; waived penalties stop counting.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        WAIVED = "waived", _("Waived")

    # The breached rule (optional — a penalty may be ad hoc). SET_NULL: deleting a rule
    # must not erase its disciplinary history.
    rule = models.ForeignKey(Rule, on_delete=models.SET_NULL, null=True, blank=True, related_name="penalties")
    # Exactly one subject (student XOR staff). Both PROTECT so the disciplinary record
    # keeps naming its subject.
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.PROTECT, null=True, blank=True, related_name="penalties"
    )
    staff = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="staff_penalties"
    )
    points = models.PositiveIntegerField()
    reason = models.CharField(max_length=255)
    # Denormalized from the student at issue time, for branch-scoped visibility.
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="penalties"
    )
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    issued_by = models.ForeignKey("users.User", on_delete=models.SET_NULL, null=True, related_name="+")
    issued_at = models.DateTimeField(auto_now_add=True)
    waived_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    waived_at = models.DateTimeField(null=True, blank=True)
    waive_reason = models.CharField(max_length=255, blank=True)
    # F24-1: set when THIS penalty pushed the student's active-point total across the
    # center's escalation threshold (an audit marker; managers were notified).
    escalated = models.BooleanField(default=False)

    class Meta:
        ordering = ("-issued_at",)
        indexes = [
            models.Index(fields=("student", "status")),
            models.Index(fields=("staff", "status")),
            models.Index(fields=("branch", "status")),
            # A director's default penalties list is whole-tenant, newest-first by issued_at;
            # the composites all lead with a subject column, so index the default sort.
            models.Index(fields=("-issued_at", "id"), name="penalty_issued_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(points__gt=0), name="penalty_points_positive"),
            # Exactly one subject: a student demerit XOR a staff-discipline penalty.
            models.CheckConstraint(
                condition=(
                    models.Q(student__isnull=False, staff__isnull=True)
                    | models.Q(student__isnull=True, staff__isnull=False)
                ),
                name="penalty_exactly_one_subject",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"penalty#{self.pk}:student#{self.student_id}:{self.points}:{self.status}"
