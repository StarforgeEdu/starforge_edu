"""Admissions CRM models.

``StudentProfile`` remains the sole identity/enrolment record. ``CRMLead`` is a
one-to-one workflow projection that adds sales/admissions state without copying
names, contacts, credentials, or safeguarding data.
"""

from __future__ import annotations

from django.db import models
from django.db.models.deletion import ProtectedError
from django.utils.translation import gettext_lazy as _


class AppendOnlyModel(models.Model):
    """Application-level guard; PostgreSQL triggers provide the hard guarantee."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk is not None and not self._state.adding:
            raise ProtectedError(str(_("Historical CRM records are append-only.")), {self})
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(str(_("Historical CRM records are append-only.")), {self})


class PipelineStage(models.Model):
    class Category(models.TextChoices):
        OPEN = "open", _("Open")
        WON = "won", _("Won")
        LOST = "lost", _("Lost")

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=8, choices=Category.choices, default=Category.OPEN)
    position = models.PositiveSmallIntegerField(unique=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("position", "id")

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    def delete(self, *args, **kwargs):
        raise ProtectedError(str(_("Pipeline stages must be retired, not deleted.")), {self})


class LeadSource(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "id")

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    def delete(self, *args, **kwargs):
        raise ProtectedError(str(_("Lead sources must be retired, not deleted.")), {self})


class AcquisitionCampaign(models.Model):
    code = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=160)
    source = models.ForeignKey(LeadSource, on_delete=models.PROTECT, related_name="campaigns")
    branch = models.ForeignKey(
        "org.Branch", on_delete=models.PROTECT, null=True, blank=True, related_name="crm_campaigns"
    )
    department = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="crm_campaigns",
    )
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "id")
        indexes = [
            models.Index(fields=("branch", "department", "is_active"), name="crm_campaign_scope_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(department__isnull=True) | models.Q(branch__isnull=False),
                name="crm_campaign_dept_needs_branch",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(starts_on__isnull=True)
                    | models.Q(ends_on__isnull=True)
                    | models.Q(ends_on__gte=models.F("starts_on"))
                ),
                name="crm_campaign_dates_ordered",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    def delete(self, *args, **kwargs):
        raise ProtectedError(str(_("Acquisition campaigns must be retired, not deleted.")), {self})


class CRMLead(models.Model):
    class State(models.TextChoices):
        OPEN = "open", _("Open")
        WON = "won", _("Won")
        LOST = "lost", _("Lost")
        MERGED = "merged", _("Merged")

    student = models.OneToOneField(
        "students.StudentProfile", on_delete=models.PROTECT, related_name="crm_lead"
    )
    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="crm_leads")
    department = models.ForeignKey(
        "org.Department", on_delete=models.PROTECT, null=True, blank=True, related_name="crm_leads"
    )
    stage = models.ForeignKey(PipelineStage, on_delete=models.PROTECT, related_name="leads")
    state = models.CharField(max_length=8, choices=State.choices, default=State.OPEN, db_index=True)
    owner = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="owned_crm_leads"
    )
    owner_principal_kind = models.CharField(max_length=16, blank=True)
    owner_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    initial_source = models.ForeignKey(
        LeadSource,
        on_delete=models.PROTECT,
        related_name="initial_leads",
    )
    initial_campaign = models.ForeignKey(
        AcquisitionCampaign,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="initial_leads",
    )
    loss_reason = models.CharField(max_length=255, blank=True)
    canonical_lead = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_duplicates"
    )
    identity_fingerprint = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    phone_fingerprint = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    email_fingerprint = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    created_by = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="created_crm_leads")
    created_by_principal_kind = models.CharField(max_length=16)
    created_by_principal_id = models.PositiveBigIntegerField()
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=("branch", "department", "state", "stage", "-created_at"),
                name="crm_lead_scope_state_idx",
            ),
            models.Index(
                fields=("owner_principal_kind", "owner_principal_id", "state", "-created_at"),
                name="crm_lead_owner_idx",
            ),
            models.Index(fields=("branch", "-created_at", "id"), name="crm_lead_branch_created_idx"),
            models.Index(fields=("-created_at", "id"), name="crm_lead_created_idx"),
            models.Index(fields=("state", "-created_at"), name="crm_lead_funnel_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        owner__isnull=True,
                        owner_principal_kind="",
                        owner_principal_id__isnull=True,
                    )
                    | (
                        models.Q(owner__isnull=False)
                        & models.Q(owner_principal_kind__in=("staff", "teacher"))
                        & models.Q(owner_principal_id__isnull=False)
                    )
                ),
                name="crm_lead_owner_principal_pair",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="lost", loss_reason__gt="")
                    | (~models.Q(state="lost") & models.Q(loss_reason=""))
                ),
                name="crm_lead_loss_reason_state",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="merged", canonical_lead__isnull=False)
                    | (~models.Q(state="merged") & models.Q(canonical_lead__isnull=True))
                ),
                name="crm_lead_canonical_state",
            ),
            models.CheckConstraint(
                condition=~models.Q(pk=models.F("canonical_lead_id")),
                name="crm_lead_not_own_canonical",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(created_by_principal_kind__in=("staff", "teacher"))
                    & models.Q(created_by_principal_id__isnull=False)
                ),
                name="crm_lead_creator_principal",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"lead#{self.pk}:{self.student_id}:{self.state}"

    def delete(self, *args, **kwargs):
        raise ProtectedError(str(_("Leads cannot be deleted; close or merge them.")), {self})


class LeadStageHistory(AppendOnlyModel):
    lead = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="stage_history")
    from_stage = models.ForeignKey(
        PipelineStage, on_delete=models.PROTECT, null=True, blank=True, related_name="stage_exits"
    )
    to_stage = models.ForeignKey(PipelineStage, on_delete=models.PROTECT, related_name="stage_entries")
    from_state = models.CharField(max_length=8, choices=CRMLead.State.choices)
    to_state = models.CharField(max_length=8, choices=CRMLead.State.choices)
    loss_reason = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=1000, blank=True)
    actor = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    actor_principal_kind = models.CharField(max_length=16)
    actor_principal_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("lead", "-created_at", "-id"), name="crm_stage_history_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(actor_principal_kind__in=("staff", "teacher")),
                name="crm_stage_actor_kind",
            )
        ]


class LeadTouch(AppendOnlyModel):
    class Channel(models.TextChoices):
        PHONE = "phone", _("Phone")
        SMS = "sms", _("SMS")
        EMAIL = "email", _("Email")
        WHATSAPP = "whatsapp", _("WhatsApp")
        IN_PERSON = "in_person", _("In person")
        OTHER = "other", _("Other")

    class Direction(models.TextChoices):
        INBOUND = "inbound", _("Inbound")
        OUTBOUND = "outbound", _("Outbound")

    lead = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="touches")
    channel = models.CharField(max_length=16, choices=Channel.choices)
    direction = models.CharField(max_length=8, choices=Direction.choices)
    outcome = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=2000)
    occurred_at = models.DateTimeField(db_index=True)
    actor = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    actor_principal_kind = models.CharField(max_length=16)
    actor_principal_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-occurred_at", "-id")
        indexes = [models.Index(fields=("lead", "-occurred_at", "-id"), name="crm_touch_timeline_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(actor_principal_kind__in=("staff", "teacher")),
                name="crm_touch_actor_kind",
            )
        ]


class LeadAttribution(AppendOnlyModel):
    lead = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="attributions")
    source = models.ForeignKey(LeadSource, on_delete=models.PROTECT, related_name="attributions")
    campaign = models.ForeignKey(
        AcquisitionCampaign,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attributions",
    )
    medium = models.CharField(max_length=64, blank=True)
    content = models.CharField(max_length=160, blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    actor = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    actor_principal_kind = models.CharField(max_length=16)
    actor_principal_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-occurred_at", "-id")
        indexes = [
            models.Index(fields=("lead", "-occurred_at", "-id"), name="crm_attribution_idx"),
            models.Index(fields=("source", "campaign", "occurred_at"), name="crm_attr_funnel_idx"),
            models.Index(fields=("campaign", "occurred_at", "lead"), name="crm_attr_campaign_idx"),
        ]


class LeadFollowUp(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending")
        COMPLETED = "completed", _("Completed")
        CANCELLED = "cancelled", _("Cancelled")

    lead = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="follow_ups")
    due_at = models.DateTimeField(db_index=True)
    purpose = models.CharField(max_length=500)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    assignee = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="crm_follow_ups")
    assignee_principal_kind = models.CharField(max_length=16)
    assignee_principal_id = models.PositiveBigIntegerField()
    created_by = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    created_by_principal_kind = models.CharField(max_length=16)
    created_by_principal_id = models.PositiveBigIntegerField()
    resolved_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    resolved_by_principal_kind = models.CharField(max_length=16, blank=True)
    resolved_by_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    resolution_note = models.CharField(max_length=1000, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("due_at", "id")
        indexes = [
            models.Index(fields=("lead", "status", "due_at", "id"), name="crm_followup_next_idx"),
            models.Index(
                fields=("assignee_principal_kind", "assignee_principal_id", "status", "due_at"),
                name="crm_followup_owner_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(assignee_principal_kind__in=("staff", "teacher")),
                name="crm_followup_assignee_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="pending",
                        resolved_by__isnull=True,
                        resolved_by_principal_kind="",
                        resolved_by_principal_id__isnull=True,
                        resolved_at__isnull=True,
                        resolution_note="",
                    )
                    | (
                        models.Q(status__in=("completed", "cancelled"))
                        & models.Q(resolved_by__isnull=False)
                        & models.Q(resolved_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(resolved_by_principal_id__isnull=False)
                        & models.Q(resolved_at__isnull=False)
                    )
                ),
                name="crm_followup_resolution_state",
            ),
        ]


class LeadDuplicateCandidate(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Pending review")
        DISMISSED = "dismissed", _("Dismissed")
        MERGED = "merged", _("Merged")

    left = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="duplicate_candidates_left")
    right = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="duplicate_candidates_right")
    score = models.PositiveSmallIntegerField()
    signals = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reviewed_by_principal_kind = models.CharField(max_length=16, blank=True)
    reviewed_by_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rationale = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ("-score", "-detected_at", "-id")
        constraints = [
            models.UniqueConstraint(fields=("left", "right"), name="crm_duplicate_pair_unique"),
            models.CheckConstraint(
                condition=models.Q(left_id__lt=models.F("right_id")), name="crm_dup_ordered"
            ),
            models.CheckConstraint(condition=models.Q(score__lte=100), name="crm_dup_score_max"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="pending",
                        reviewed_by__isnull=True,
                        reviewed_by_principal_kind="",
                        reviewed_by_principal_id__isnull=True,
                        reviewed_at__isnull=True,
                        rationale="",
                    )
                    | (
                        models.Q(status__in=("dismissed", "merged"))
                        & models.Q(reviewed_by__isnull=False)
                        & models.Q(reviewed_by_principal_kind__in=("staff", "teacher"))
                        & models.Q(reviewed_by_principal_id__isnull=False)
                        & models.Q(reviewed_at__isnull=False)
                        & models.Q(rationale__gt="")
                    )
                ),
                name="crm_duplicate_review_state",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "-score", "-detected_at"), name="crm_duplicate_review_idx"),
        ]


class LeadMerge(AppendOnlyModel):
    candidate = models.OneToOneField(
        LeadDuplicateCandidate, on_delete=models.PROTECT, related_name="merge_record"
    )
    canonical = models.ForeignKey(CRMLead, on_delete=models.PROTECT, related_name="canonical_merges")
    duplicate = models.OneToOneField(CRMLead, on_delete=models.PROTECT, related_name="merge_as_duplicate")
    rationale = models.CharField(max_length=1000)
    reviewed_by = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    reviewed_by_principal_kind = models.CharField(max_length=16)
    reviewed_by_principal_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(canonical=models.F("duplicate")), name="crm_merge_distinct_leads"
            )
        ]


class CRMIdempotencyRecord(AppendOnlyModel):
    actor = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="+")
    actor_principal_kind = models.CharField(max_length=16)
    actor_principal_id = models.PositiveBigIntegerField()
    key_hash = models.CharField(max_length=64)
    operation = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)
    result_type = models.CharField(max_length=32)
    result_id = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("actor_principal_kind", "actor_principal_id", "key_hash"),
                name="crm_idempotency_actor_key",
            )
        ]
        indexes = [models.Index(fields=("created_at",), name="crm_idempotency_age_idx")]
