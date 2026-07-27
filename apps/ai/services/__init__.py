"""AI write-side services (TASKS §18, D4-LA-4/8).

All AI execution is Celery-only and budget-gated. The two load-bearing
primitives here are:

- ``check_and_reserve_budget`` — pre-flight: under ``select_for_update`` on the
  singleton ``TenantAIBudget`` row it rolls day/month anchors over, rejects an
  over-budget / disabled request (recording a ``denied_budget`` ``AIRequest``),
  and otherwise creates a ``queued`` ``AIRequest`` (idempotent on the request's
  idempotency key).
- ``record_usage`` — post-completion reconciliation: atomically bumps the budget
  counters and the request's token/cost columns with ``F()`` expressions, guarded
  by status so a Celery retry never double-counts.

No HTTP, no Anthropic import, no redaction here — those live in
``celery_tasks/ai_tasks.py`` and ``apps/ai/redaction.py`` respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import status

from apps.ai.models import AIFeature, AIPrompt, AIRequest, TenantAIBudget
from core.exceptions import StarforgeError, ValidationException

_MTOK = Decimal(1_000_000)


class AIBudgetExceeded(StarforgeError):
    """429-style envelope when a request would exceed the daily/monthly token
    budget or the budget is disabled (D4-LA-4)."""

    code = "ai_budget_exceeded"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = _("The AI token budget for this period has been exhausted.")


class AIFeatureDisabled(StarforgeError):
    """403 when a feature is gated off via CenterSettings (D4-LA-7)."""

    code = "feature_disabled"
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _("This AI feature is disabled for your center.")


@dataclass(frozen=True)
class Usage:
    """Token usage returned by the Anthropic client / mock."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @classmethod
    def from_dict(cls, raw: dict) -> Usage:
        return cls(
            input_tokens=int(raw.get("input_tokens", 0)),
            output_tokens=int(raw.get("output_tokens", 0)),
            cache_read_tokens=int(raw.get("cache_read_input_tokens", raw.get("cache_read_tokens", 0))),
            cache_creation_tokens=int(
                raw.get("cache_creation_input_tokens", raw.get("cache_creation_tokens", 0))
            ),
        )

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def cost_microusd(usage: Usage) -> int:
    """Placeholder cost model (TD-13): input/output priced per million tokens
    from ``settings.AI_COST_PER_MTOK_*`` (real pricing is [OWNER:O-2])."""
    inp = settings.AI_COST_PER_MTOK_INPUT_MICROUSD
    out = settings.AI_COST_PER_MTOK_OUTPUT_MICROUSD
    total = (Decimal(usage.input_tokens) / _MTOK) * Decimal(inp) + (
        Decimal(usage.output_tokens) / _MTOK
    ) * Decimal(out)
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _get_budget_locked() -> TenantAIBudget:
    """Fetch (creating if absent) the singleton budget row FOR UPDATE, rolling
    day/month counters over when the active date crosses the stored anchor."""
    TenantAIBudget.objects.get_or_create(pk=1)
    budget = TenantAIBudget.objects.select_for_update().get(pk=1)
    today = timezone.localdate()
    dirty: list[str] = []
    if budget.day_anchor != today:
        budget.tokens_used_today = 0
        budget.day_anchor = today
        dirty += ["tokens_used_today", "day_anchor"]
    if (budget.month_anchor.year, budget.month_anchor.month) != (today.year, today.month):
        budget.tokens_used_month = 0
        budget.month_anchor = today
        dirty += ["tokens_used_month", "month_anchor"]
    if dirty:
        budget.save(update_fields=[*dirty, "updated_at"])
    return budget


def active_prompt(feature: str) -> AIPrompt:
    """The active prompt version for ``feature`` (422 if none seeded)."""
    try:
        return AIPrompt.objects.get(feature=feature, is_active=True)
    except AIPrompt.DoesNotExist as exc:
        raise ValidationException(
            _("No active AI prompt is configured for this feature."),
            code="ai_prompt_missing",
        ) from exc


def make_idempotency_key(
    *, feature: str, source_app: str, source_id: int, version: int, params: dict | None = None
) -> str:
    """The idempotency key for one AI run.

    Base key = ``feature:source_app:source_id:v{version}``. When ``params`` is given
    (R3-P3), a stable hash of them is appended so two requests on the SAME source row but
    with DIFFERENT generation parameters (e.g. exam difficulty / question_count) get
    DISTINCT keys — otherwise the second silently returns the first's stale result. Passing
    no params leaves the key unchanged (backward-compatible: the other AI features that key
    purely on their source row are unaffected)."""
    base = f"{feature}:{source_app}:{source_id}:v{version}"
    if not params:
        return base
    import hashlib
    import json

    digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"{base}:h{digest}"


# Terminal-FAILURE states that a re-request may re-drive (reset to queued + re-reserve):
# a budget denial is transient (budget rolls over / is re-enabled) and a failed run is
# retryable. A succeeded/queued/running row is returned unchanged (idempotent).
_RE_DRIVABLE_STATUSES = (AIRequest.Status.DENIED_BUDGET, AIRequest.Status.FAILED)


def check_and_reserve_budget(
    *,
    feature: str,
    estimated_tokens: int,
    requested_by=None,
    requested_by_id: int | None = None,
    source_app: str,
    source_id: int,
    params: dict | None = None,
) -> AIRequest:
    """Reserve budget and create a ``queued`` ``AIRequest`` for one feature run.

    Accepts either ``requested_by`` (a User instance, from a request handler) or
    ``requested_by_id`` (an int, from a Celery task carrying only the id).

    - Idempotent: a duplicate (feature, source_app, source_id, active version [, params])
      returns the existing row and reserves nothing again. ``params`` (optional, R3-P3)
      distinguishes runs on the same source row that differ by generation parameters.
    - Over-budget or disabled: records an ``AIRequest(status=denied_budget)`` and
      raises ``AIBudgetExceeded`` (429 envelope, code ``ai_budget_exceeded``).

    NOT wrapped in a single ``@transaction.atomic`` for the whole body: the denial
    row MUST survive the raised exception, so it is committed in its own atomic
    block before raising (an outer rollback would otherwise discard it).
    """
    prompt = active_prompt(feature)
    key = make_idempotency_key(
        feature=feature, source_app=source_app, source_id=source_id, version=prompt.version, params=params
    )
    actor_id = requested_by_id if requested_by_id is not None else getattr(requested_by, "id", None)
    requested = max(int(estimated_tokens), 0)

    def _create(status: str, *, reserved: int = 0) -> tuple[AIRequest, bool]:
        # get_or_create on the unique key makes a concurrent duplicate a no-op.
        return AIRequest.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "feature": feature,
                "status": status,
                "prompt": prompt,
                "requested_by_id": actor_id,
                "source_app": source_app,
                "source_id": source_id,
                "reserved_tokens": reserved,
            },
        )

    with transaction.atomic():
        # Lock the budget first so the existing-row read + any reservation are
        # serialized (a concurrent duplicate / re-drive can't double-spend).
        budget = _get_budget_locked()
        existing = AIRequest.objects.filter(idempotency_key=key).first()
        if existing is not None and existing.status not in _RE_DRIVABLE_STATUSES:
            existing._should_enqueue = False
            return existing  # in-flight or already succeeded — idempotent no-op
        disabled = not budget.is_enabled
        over_daily = budget.tokens_used_today + requested > budget.daily_token_limit
        over_monthly = budget.tokens_used_month + requested > budget.monthly_token_limit
        if not (disabled or over_daily or over_monthly):
            # Within budget: RESERVE the estimate against the budget while still
            # holding the lock, so a burst of in-flight requests can't all pass the
            # same stale check and collectively over-spend. record_usage reconciles
            # the delta to real usage; a failure/cache-hit releases it.
            if existing is not None:
                # Re-drive a previously denied/failed request now that there IS
                # budget — a transient denial must not strand the source forever.
                existing.status = AIRequest.Status.QUEUED
                existing.reserved_tokens = requested
                existing.error_detail = ""
                existing.requested_by_id = actor_id
                existing.save(update_fields=["status", "reserved_tokens", "error_detail", "requested_by_id"])
                obj, created = existing, True
            else:
                obj, created = _create(AIRequest.Status.QUEUED, reserved=requested)
            if created and requested:  # never double-reserve a concurrent duplicate
                TenantAIBudget.objects.filter(pk=budget.pk).update(
                    tokens_used_today=F("tokens_used_today") + requested,
                    tokens_used_month=F("tokens_used_month") + requested,
                    updated_at=timezone.now(),
                )
            # Callers enqueue only when this invocation created or deliberately
            # re-drove the request. Returning an existing QUEUED row is a no-op.
            obj._should_enqueue = created
            return obj

    # Over budget / disabled: the lock is released; record (or keep) the denial in
    # its own committed transaction so it persists, then raise the 429 envelope. A
    # denial reserves nothing (reserved_tokens stays 0).
    with transaction.atomic():
        _create(AIRequest.Status.DENIED_BUDGET)
    if disabled:
        raise AIBudgetExceeded(_("AI is disabled for this center."), code="ai_budget_exceeded")
    raise AIBudgetExceeded(
        _("The AI token budget for this period has been exhausted."),
        code="ai_budget_exceeded",
    )


@transaction.atomic
def record_usage(*, ai_request_id: int, usage: Usage, billable: bool = True) -> None:
    """Reconcile real token usage onto the request + the tenant budget.

    The request reserved ``reserved_tokens`` (the estimate) at queue time; here we
    move the budget by the *delta* (actual - reserved) and zero the reservation,
    so the net effect of reserve+reconcile equals the real usage. Guarded by
    status so a retried task never double-reconciles.

    ``billable=False`` (a Redis response-cache hit — no tokens were actually
    purchased) records the usage columns for transparency but bills ZERO: the
    reservation is fully released and ``cost_microusd`` stays 0.
    """
    req = AIRequest.objects.select_for_update().get(pk=ai_request_id)
    if req.status not in (AIRequest.Status.RUNNING, AIRequest.Status.QUEUED):
        return  # already reconciled / terminal — idempotent no-op on retry

    reserved = req.reserved_tokens
    billed = usage.total if billable else 0

    req.input_tokens = usage.input_tokens
    req.output_tokens = usage.output_tokens
    req.cache_read_tokens = usage.cache_read_tokens
    req.cache_creation_tokens = usage.cache_creation_tokens
    req.cost_microusd = cost_microusd(usage) if billable else 0
    req.reserved_tokens = 0  # reservation consumed by this reconciliation
    req.save(
        update_fields=[
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cost_microusd",
            "reserved_tokens",
        ]
    )

    # Reconcile under the lock: replace the reserved estimate with the billed
    # amount. Clamp at 0 so a day/month rollover between reserve and reconcile
    # can't drive the counter negative.
    budget = _get_budget_locked()
    delta = billed - reserved
    budget.tokens_used_today = max(0, budget.tokens_used_today + delta)
    budget.tokens_used_month = max(0, budget.tokens_used_month + delta)
    budget.save(update_fields=["tokens_used_today", "tokens_used_month", "updated_at"])


@transaction.atomic
def release_reservation(*, ai_request_id: int) -> None:
    """Return a request's outstanding reservation to the budget (terminal failure).

    Idempotent: a request that already reconciled (record_usage zeroed
    ``reserved_tokens``) releases nothing. Clamps at 0 against a rollover."""
    req = AIRequest.objects.select_for_update().get(pk=ai_request_id)
    reserved = req.reserved_tokens
    if reserved <= 0:
        return
    req.reserved_tokens = 0
    req.save(update_fields=["reserved_tokens"])
    budget = _get_budget_locked()
    budget.tokens_used_today = max(0, budget.tokens_used_today - reserved)
    budget.tokens_used_month = max(0, budget.tokens_used_month - reserved)
    budget.save(update_fields=["tokens_used_today", "tokens_used_month", "updated_at"])


@transaction.atomic
def update_budget(*, daily_token_limit=None, monthly_token_limit=None, is_enabled=None) -> TenantAIBudget:
    """Director-only mutation of the budget limits / enabled flag (D4-LA-8)."""
    budget = _get_budget_locked()
    fields: list[str] = []
    if daily_token_limit is not None:
        budget.daily_token_limit = int(daily_token_limit)
        fields.append("daily_token_limit")
    if monthly_token_limit is not None:
        budget.monthly_token_limit = int(monthly_token_limit)
        fields.append("monthly_token_limit")
    if is_enabled is not None:
        budget.is_enabled = bool(is_enabled)
        fields.append("is_enabled")
    if fields:
        budget.save(update_fields=[*fields, "updated_at"])
    return budget


def request_exam_generation(
    *,
    subject_id: int,
    exam_type: str,
    question_count: int,
    difficulty: str,
    requested_by=None,
) -> AIRequest:
    """Request-driven exam generation (D4-LA-8). Gated by
    ``CenterSettings.ai_exam_generation_enabled`` (TD-13), then budget-reserved
    and enqueued on commit. The Subject id is the source row for idempotency.

    NOT ``@transaction.atomic`` at this level: ``check_and_reserve_budget`` owns
    its own transactions so the ``denied_budget`` row survives a raised
    ``AIBudgetExceeded`` (an enclosing atomic here would roll it back)."""
    from apps.org.selectors import get_center_settings

    if not get_center_settings().ai_exam_generation_enabled:
        raise AIFeatureDisabled(code="feature_disabled")

    # The active prompt's cap is the budget estimate (TD-13: no magic number).
    prompt = active_prompt(AIFeature.EXAM_GENERATION)
    ai_request = check_and_reserve_budget(
        feature=AIFeature.EXAM_GENERATION,
        estimated_tokens=prompt.token_cost_cap,
        requested_by=requested_by,
        source_app="academics",
        source_id=subject_id,
        # R3-P3: the exam-shape params are part of the idempotency identity, so a second
        # request for the same subject with a different type/count/difficulty generates a
        # NEW exam instead of silently returning the first result.
        params={"exam_type": exam_type, "question_count": question_count, "difficulty": difficulty},
    )

    if getattr(ai_request, "_should_enqueue", False):
        from core.utils import current_schema

        schema = current_schema()
        params = {
            "subject_id": subject_id,
            "exam_type": exam_type,
            "question_count": question_count,
            "difficulty": difficulty,
        }
        transaction.on_commit(lambda: _enqueue_exam_generation(ai_request.pk, params, schema))
    return ai_request


def _enqueue_exam_generation(ai_request_id: int, params: dict, schema: str) -> None:
    from celery_tasks.ai_tasks import run_exam_generation

    run_exam_generation.delay(ai_request_id, params=params, _schema_name=schema)
