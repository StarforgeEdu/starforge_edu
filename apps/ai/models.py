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

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.fields import EncryptedTextField


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
        ]
        indexes = [models.Index(fields=("feature", "is_active"))]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.feature} v{self.version}{' (active)' if self.is_active else ''}"


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

    feature = models.CharField(max_length=32, choices=AIFeature.choices, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True)
    prompt = models.ForeignKey(AIPrompt, on_delete=models.PROTECT, related_name="requests")
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    source_app = models.CharField(max_length=32)
    source_id = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=128, unique=True)

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
    output_text = models.TextField(blank=True)
    error_detail = models.TextField(blank=True)
    celery_task_id = models.CharField(max_length=64, blank=True)

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
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"AIRequest#{self.pk} {self.feature}:{self.status}"
