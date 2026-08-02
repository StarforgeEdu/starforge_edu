"""AI subsystem models (TASKS §18, D4-LA-1).

Three tenant-schema models replace the `AiItem` placeholder:

- ``TenantAIBudget`` — singleton per tenant (pk=1) holding day/month token caps
  plus rolling usage counters with day/month anchors.
- ``AIRequest`` — one row per AI feature invocation; carries status, redaction
  map (encrypted, TD-11), token/cost accounting, and an idempotency key that
  makes duplicate signal deliveries no-ops.
- ``AIPrompt`` — versioned prompt templates, one active version per feature.

All AI execution is Celery-only and goes through
``infrastructure/ai/anthropic_client.complete`` after a ``TenantAIBudget``
pre-flight check (DoD #9).
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.fields import EncryptedTextField


def ai_content_expiry():
    """Default deadline for deleting generated text and reversible PII maps.

    Accounting, immutable scope, prompt version, and provider receipt metadata are
    retained as operational evidence.  Generated content has a much shorter privacy
    lifetime and is removed independently by the AI maintenance task.
    """

    days = int(getattr(settings, "AI_CONTENT_RETENTION_DAYS", 30))
    return timezone.now() + timedelta(days=max(1, min(days, 365)))


class AIFeature(models.TextChoices):
    ASSIGNMENT_FEEDBACK = "assignment_feedback", _("Assignment feedback")
    EXAM_GENERATION = "exam_generation", _("Exam generation")
    CONTENT_SUMMARY = "content_summary", _("Content summary")
    PLACEMENT_GENERATION = "placement_generation", _("Placement test generation")
    FORM_ANALYSIS = "form_analysis", _("Form response analysis")
    WRITING_MARKING = "writing_marking", _("Placement writing marking")
    MATERIAL_GENERATION = "material_generation", _("Library material generation")
    TEMPLATE_GENERATION = "template_generation", _("Message template generation")


class TenantAIBudget(models.Model):
    """Per-tenant token budget singleton (pk=1).

    Counters roll over when the active date crosses the stored anchor — the
    budget service (``record_usage`` / ``check_and_reserve_budget``) resets the
    today/month counters under ``select_for_update`` so a date change never
    double-charges or carries stale usage.
    """

    daily_token_limit = models.PositiveIntegerField(default=settings.AI_DEFAULT_DAILY_TOKENS)
    monthly_token_limit = models.PositiveIntegerField(default=settings.AI_DEFAULT_MONTHLY_TOKENS)
    tokens_used_today = models.PositiveBigIntegerField(default=0)
    tokens_used_month = models.PositiveBigIntegerField(default=0)
    day_anchor = models.DateField(default=timezone.localdate)
    month_anchor = models.DateField(default=timezone.localdate)
    is_enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Tenant AI budget")
        verbose_name_plural = _("Tenant AI budgets")
        ordering = ("pk",)
        constraints = [
            # Singleton: only one budget row may exist per tenant schema.
            models.CheckConstraint(condition=models.Q(pk=1), name="ai_budget_singleton_pk1"),
            models.CheckConstraint(
                condition=models.Q(monthly_token_limit__gte=models.F("daily_token_limit")),
                name="ai_budget_monthly_not_below_daily",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"AIBudget(day={self.tokens_used_today}/{self.daily_token_limit})"


class AIPrompt(models.Model):
    """A versioned prompt template for one AI feature.

    Exactly one version per feature may be active at a time (partial unique
    constraint). Seeded with one active prompt per feature in a data migration.
    """

    feature = models.CharField(max_length=32, choices=AIFeature.choices, db_index=True)
    version = models.PositiveSmallIntegerField()
    system_prompt = models.TextField()
    user_template = models.TextField()
    max_output_tokens = models.PositiveIntegerField()
    effort = models.CharField(max_length=16, default="medium")
    token_cost_cap = models.PositiveIntegerField()
    is_active = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("AI prompt")
        verbose_name_plural = _("AI prompts")
        ordering = ("feature", "-version")
        constraints = [
            models.UniqueConstraint(fields=("feature", "version"), name="ai_prompt_unique_feature_version"),
            models.UniqueConstraint(
                fields=("feature",),
                condition=models.Q(is_active=True),
                name="ai_prompt_one_active_per_feature",
            ),
            models.CheckConstraint(
                condition=models.Q(effort__in=("low", "medium", "high", "max")),
                name="ai_prompt_effort_supported",
            ),
            models.CheckConstraint(
                condition=models.Q(max_output_tokens__gt=0),
                name="ai_prompt_output_tokens_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(token_cost_cap__gte=models.F("max_output_tokens")),
                name="ai_prompt_cost_cap_covers_output",
            ),
        ]
        indexes = [models.Index(fields=("feature", "is_active"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.feature} v{self.version}{' (active)' if self.is_active else ''}"

    def save(self, *args, **kwargs):
        # A request points at a prompt version as immutable execution evidence.
        # Editing that version in place would rewrite history; only activation
        # may change. Content changes require a new version row.
        if self.pk:
            immutable = (
                "feature",
                "version",
                "system_prompt",
                "user_template",
                "max_output_tokens",
                "effort",
                "token_cost_cap",
            )
            previous = type(self).objects.filter(pk=self.pk).values(*immutable).first()
            if previous is not None and any(previous[field] != getattr(self, field) for field in immutable):
                raise ValidationError(
                    {"version": [_("Create a new AI prompt version instead of editing history.")]}
                )
        return super().save(*args, **kwargs)


class AIRequest(models.Model):
    """One AI feature invocation, queued/run via Celery.

    ``idempotency_key`` (``feature:source_app:source_id:vN``) is unique, so a
    duplicate signal delivery resolves to the same row instead of a second job.
    Token/cost columns are reconciled post-completion via ``record_usage``.
    """

    # Transient service-layer signal; never persisted. It tells request entrypoints
    # whether this invocation created/re-drove the row and therefore owns enqueueing.
    _should_enqueue: bool

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        RUNNING = "running", _("Running")
        SUCCEEDED = "succeeded", _("Succeeded")
        FAILED = "failed", _("Failed")
        DENIED_BUDGET = "denied_budget", _("Denied (budget)")
        UNCERTAIN = "uncertain", _("Provider outcome requires review")

    class AttributionStatus(models.TextChoices):
        RESOLVED = "resolved", _("Resolved")
        UNRESOLVED = "unresolved", _("Unresolved")

    class ScopeStatus(models.TextChoices):
        ORGANIZATION = "organization", _("Organization-wide")
        RESOLVED = "resolved", _("Resolved")
        UNRESOLVED = "unresolved", _("Unresolved")

    feature = models.CharField(max_length=32, choices=AIFeature.choices, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    prompt = models.ForeignKey(AIPrompt, on_delete=models.PROTECT, related_name="requests")
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # ``User`` is an internal compatibility bridge and may back several role
    # accounts.  These immutable fields identify the exact requesting principal.
    requested_principal_kind = models.CharField(max_length=16, blank=True, editable=False)
    requested_principal_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    attribution_status = models.CharField(
        max_length=16,
        choices=AttributionStatus.choices,
        default=AttributionStatus.UNRESOLVED,
        editable=False,
    )
    # Historical authorization ownership is captured at queue time.  Request logs
    # and worker re-authorization use this snapshot, never a source row's current
    # placement after a transfer.
    scope_status = models.CharField(
        max_length=16,
        choices=ScopeStatus.choices,
        default=ScopeStatus.UNRESOLVED,
        editable=False,
    )
    branch_at_request = models.ForeignKey(
        "org.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ai_requests",
        editable=False,
    )
    department_at_request = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ai_requests",
        editable=False,
    )
    authorization_permission = models.CharField(max_length=64, blank=True, editable=False)
    source_app = models.CharField(max_length=32)
    source_id = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=128, unique=True)
    parameter_fingerprint = models.CharField(max_length=64, blank=True, editable=False)

    # Tokens reserved against the budget at queue time (the prompt's cost cap).
    # record_usage reconciles the delta to actual usage; a terminal failure
    # releases the remainder. Non-zero only while a request is in flight.
    reserved_tokens = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_creation_tokens = models.PositiveIntegerField(default=0)
    cost_microusd = models.BigIntegerField(default=0)

    # TD-11: the PII redaction map (token -> original) is encrypted at rest.
    redaction_map = EncryptedTextField(blank=True, default="")
    # Expand/contract privacy migration: legacy ``output_text`` remains as an
    # always-empty compatibility column for one release.  New code reads/writes
    # only the authenticated ciphertext field; the DB constraint prevents old
    # application nodes from reintroducing plaintext after cutover.
    output_ciphertext = EncryptedTextField(blank=True, default="")
    output_text = models.TextField(blank=True, default="", editable=False)
    # Stable internal code only. Provider exception strings can contain tenant
    # text, URLs, or credentials and must never be persisted here.
    error_detail = models.CharField(max_length=64, blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)
    # Written immediately before the external call. Once present it proves that
    # a retry cannot know whether the provider accepted/billed the request unless
    # ``provider_request_id`` and encrypted output were durably committed.
    provider_attempt_id = models.CharField(max_length=64, blank=True, editable=False)
    provider_attempted_at = models.DateTimeField(null=True, blank=True, editable=False)
    provider_request_id = models.CharField(max_length=255, blank=True, editable=False)
    provider_stop_reason = models.CharField(max_length=32, blank=True, editable=False)
    provider_reconciliation_status = models.CharField(
        max_length=16,
        choices=(("not_charged", "Not charged"), ("charged", "Charged")),
        blank=True,
        editable=False,
    )
    provider_reconciliation_reference = models.CharField(
        max_length=128,
        blank=True,
        editable=False,
    )
    provider_reconciled_at = models.DateTimeField(null=True, blank=True, editable=False)
    content_expires_at = models.DateTimeField(default=ai_content_expiry, db_index=True, editable=False)
    content_purged_at = models.DateTimeField(null=True, blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("AI request")
        verbose_name_plural = _("AI requests")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("feature", "status")),
            models.Index(fields=("source_app", "source_id")),
            models.Index(fields=("created_at",)),
            models.Index(
                fields=("scope_status", "branch_at_request", "department_at_request", "created_at"),
                name="ai_request_scope_created_idx",
            ),
            models.Index(
                fields=("requested_principal_kind", "requested_principal_id", "created_at"),
                name="ai_request_principal_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status="resolved",
                        requested_by__isnull=False,
                        requested_principal_kind__in=("staff", "teacher", "student", "parent"),
                        requested_principal_id__isnull=False,
                        scope_status__in=("organization", "resolved"),
                    )
                    & ~models.Q(authorization_permission="")
                    | models.Q(
                        attribution_status="unresolved",
                        requested_principal_kind="",
                        requested_principal_id__isnull=True,
                        authorization_permission="",
                        scope_status="unresolved",
                    )
                ),
                name="ai_request_attribution_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        scope_status="organization",
                        branch_at_request__isnull=True,
                        department_at_request__isnull=True,
                    )
                    | models.Q(
                        scope_status="resolved",
                        branch_at_request__isnull=False,
                    )
                    | models.Q(
                        scope_status="unresolved",
                        branch_at_request__isnull=True,
                        department_at_request__isnull=True,
                    )
                ),
                name="ai_request_scope_consistent",
            ),
            models.CheckConstraint(
                condition=models.Q(output_text=""),
                name="ai_request_no_plaintext_output",
            ),
            models.CheckConstraint(
                condition=models.Q(cost_microusd__gte=0),
                name="ai_request_cost_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(provider_attempt_id="", provider_attempted_at__isnull=True)
                    | (~models.Q(provider_attempt_id="") & models.Q(provider_attempted_at__isnull=False))
                ),
                name="ai_request_provider_attempt_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(provider_request_id="")
                    | (
                        ~models.Q(provider_attempt_id="")
                        & models.Q(provider_attempted_at__isnull=False)
                        & models.Q(
                            provider_stop_reason__in=(
                                "end_turn",
                                "max_tokens",
                                "stop_sequence",
                                "refusal",
                            )
                        )
                    )
                ),
                name="ai_request_provider_receipt_has_attempt",
            ),
            models.CheckConstraint(
                condition=(~models.Q(provider_request_id="") | models.Q(provider_stop_reason="")),
                name="ai_request_stop_reason_requires_receipt",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        provider_reconciliation_status="",
                        provider_reconciliation_reference="",
                        provider_reconciled_at__isnull=True,
                    )
                    | (
                        models.Q(
                            provider_reconciliation_status__in=("not_charged", "charged"),
                            provider_reconciled_at__isnull=False,
                        )
                        & ~models.Q(provider_reconciliation_reference="")
                    )
                ),
                name="ai_request_reconciliation_evidence",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(provider_reconciliation_status="not_charged") | models.Q(provider_request_id="")
                ),
                name="ai_request_not_charged_has_no_receipt",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(provider_reconciliation_status="charged") | ~models.Q(provider_request_id="")
                ),
                name="ai_request_charged_has_receipt",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="uncertain")
                    | (
                        ~models.Q(provider_attempt_id="")
                        & models.Q(provider_request_id="")
                        & models.Q(reserved_tokens__gt=0)
                    )
                ),
                name="ai_request_uncertain_reserves_budget",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="queued",
                        reserved_tokens__gt=0,
                        provider_attempt_id="",
                        provider_request_id="",
                    )
                    | (models.Q(status="running", provider_request_id="") & models.Q(reserved_tokens__gt=0))
                    | (
                        models.Q(status="running")
                        & ~models.Q(provider_request_id="")
                        & models.Q(reserved_tokens=0)
                    )
                    | models.Q(
                        status="uncertain",
                        reserved_tokens__gt=0,
                        provider_request_id="",
                    )
                    | models.Q(
                        status__in=("succeeded", "failed", "denied_budget"),
                        reserved_tokens=0,
                    )
                ),
                name="ai_request_reservation_state_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(provider_attempt_id="")
                    | ~models.Q(provider_request_id="")
                    | (models.Q(status__in=("running", "uncertain")) & models.Q(reserved_tokens__gt=0))
                    | models.Q(
                        status="failed",
                        reserved_tokens=0,
                        provider_reconciliation_status="not_charged",
                    )
                ),
                name="ai_request_ambiguous_attempt_not_released",
            ),
        ]

    _IMMUTABLE_FIELDS = (
        "feature",
        "prompt_id",
        "requested_by_id",
        "requested_principal_kind",
        "requested_principal_id",
        "attribution_status",
        "scope_status",
        "branch_at_request_id",
        "department_at_request_id",
        "authorization_permission",
        "source_app",
        "source_id",
        "idempotency_key",
        "parameter_fingerprint",
        "content_expires_at",
    )
    _ACCOUNTING_FIELDS = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost_microusd",
    )

    def __str__(self) -> str:  # pragma: no cover
        return f"AIRequest#{self.pk} {self.feature}:{self.status}"

    def save(self, *args, **kwargs):
        # Compatibility callers/tests that still assign output_text are upgraded
        # before SQL; plaintext never reaches the constrained legacy column.
        if self.output_text:
            if self.output_ciphertext and self.output_ciphertext != self.output_text:
                raise ValidationError({"output_text": [_("Conflicting AI output values.")]})
            self.output_ciphertext = self.output_text
            self.output_text = ""
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = tuple(
                    (set(update_fields) - {"output_text"}) | {"output_ciphertext", "output_text"}
                )
        if self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    *self._IMMUTABLE_FIELDS,
                    *self._ACCOUNTING_FIELDS,
                    "status",
                    "provider_attempt_id",
                    "provider_attempted_at",
                    "provider_request_id",
                    "provider_stop_reason",
                    "provider_reconciliation_status",
                    "provider_reconciliation_reference",
                    "provider_reconciled_at",
                )
                .first()
            )
            if previous is not None:
                changed = [
                    field for field in self._IMMUTABLE_FIELDS if previous[field] != getattr(self, field)
                ]
                if changed:
                    raise ValidationError(
                        {
                            field.removesuffix("_id"): [_("AI request attribution is immutable.")]
                            for field in changed
                        }
                    )
                if (
                    previous["provider_attempt_id"]
                    and previous["provider_attempt_id"] != self.provider_attempt_id
                ):
                    raise ValidationError({"provider_attempt_id": [_("The provider attempt is immutable.")]})
                if (
                    previous["provider_attempted_at"] is not None
                    and previous["provider_attempted_at"] != self.provider_attempted_at
                ):
                    raise ValidationError(
                        {"provider_attempted_at": [_("The provider attempt is immutable.")]}
                    )
                if (
                    previous["provider_request_id"]
                    and previous["provider_request_id"] != self.provider_request_id
                ):
                    raise ValidationError({"provider_request_id": [_("The provider receipt is immutable.")]})
                if (
                    previous["provider_stop_reason"]
                    and previous["provider_stop_reason"] != self.provider_stop_reason
                ):
                    raise ValidationError(
                        {"provider_stop_reason": [_("The provider stop reason is immutable.")]}
                    )
                reconciliation_was_set = bool(previous["provider_reconciliation_status"])
                if reconciliation_was_set and (
                    previous["provider_reconciliation_status"] != self.provider_reconciliation_status
                    or previous["provider_reconciliation_reference"] != self.provider_reconciliation_reference
                    or previous["provider_reconciled_at"] != self.provider_reconciled_at
                ):
                    raise ValidationError(
                        {
                            "provider_reconciliation_status": [
                                _("The provider reconciliation evidence is immutable.")
                            ]
                        }
                    )
                is_charged_reconciliation = (
                    previous["status"] == self.Status.UNCERTAIN
                    and not previous["provider_request_id"]
                    and not previous["provider_reconciliation_status"]
                    and self.provider_reconciliation_status == "charged"
                    and bool(self.provider_request_id)
                )
                accounting_was_final = not is_charged_reconciliation and (
                    bool(previous["provider_request_id"])
                    or bool(previous["provider_reconciliation_status"])
                    or previous["status"]
                    in (
                        self.Status.SUCCEEDED,
                        self.Status.FAILED,
                        self.Status.DENIED_BUDGET,
                    )
                    or self.status
                    in (
                        self.Status.SUCCEEDED,
                        self.Status.FAILED,
                        self.Status.DENIED_BUDGET,
                    )
                )
                accounting_changed = [
                    field for field in self._ACCOUNTING_FIELDS if previous[field] != getattr(self, field)
                ]
                if accounting_was_final and accounting_changed:
                    raise ValidationError(
                        {
                            field: [_("Final AI provider accounting is immutable.")]
                            for field in accounting_changed
                        }
                    )
        return super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        errors: dict[str, list[str]] = {}
        if self.department_at_request_id is not None:
            department = self.department_at_request
            if (
                department is None
                or self.branch_at_request_id is None
                or department.branch_id != self.branch_at_request_id
            ):
                errors["department_at_request"] = [
                    str(_("The department must belong to the captured branch."))
                ]
        if self.attribution_status == self.AttributionStatus.RESOLVED and not self.authorization_permission:
            errors["authorization_permission"] = [str(_("A resolved request needs an authorization code."))]
        if errors:
            raise ValidationError(errors)

    @property
    def protected_output(self) -> str:
        """Decrypted generated content at the trusted model boundary."""

        return self.output_ciphertext or ""
