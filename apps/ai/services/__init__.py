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

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from string import Formatter
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import status

from apps.ai.models import AIFeature, AIPrompt, AIRequest, TenantAIBudget
from core.exceptions import ConflictException, StarforgeError, ValidationException
from core.role_principals import RolePrincipal

_MTOK = Decimal(1_000_000)
_PROVIDER_STOP_REASONS = frozenset({"end_turn", "max_tokens", "stop_sequence", "refusal"})
_PROMPT_FIELDS: dict[str, frozenset[str]] = {
    AIFeature.ASSIGNMENT_FEEDBACK: frozenset({"assignment_title", "submission_text", "student_name"}),
    AIFeature.EXAM_GENERATION: frozenset({"subject_name", "exam_type", "question_count", "difficulty"}),
    AIFeature.CONTENT_SUMMARY: frozenset({"file_title", "file_type"}),
    AIFeature.PLACEMENT_GENERATION: frozenset({"subject", "count", "difficulty", "topic"}),
    AIFeature.FORM_ANALYSIS: frozenset({"form_title", "aggregate", "comments"}),
    AIFeature.WRITING_MARKING: frozenset({"items"}),
    AIFeature.MATERIAL_GENERATION: frozenset({"title", "topic"}),
    AIFeature.TEMPLATE_GENERATION: frozenset({"name", "purpose"}),
}


class AIBudgetExceeded(StarforgeError):
    """429-style envelope when a request would exceed the daily/monthly token
    budget or the budget is disabled (D4-LA-4)."""

    code = "ai_budget_exceeded"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = _("The AI token budget for this period has been exhausted.")


class AIFeatureDisabled(StarforgeError):
    """403 when AI or one AI feature is disabled (D4-LA-7)."""

    code = "feature_disabled"
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _("This AI feature is currently disabled.")


def ai_enabled() -> bool:
    """Global operator switch for all model-provider work."""
    return bool(getattr(settings, "AI_ENABLED", True))


def ensure_ai_enabled() -> None:
    if not ai_enabled():
        raise AIFeatureDisabled()


@dataclass(frozen=True)
class Usage:
    """Token usage returned by the Anthropic client / mock."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
        )
        max_tokens = int(getattr(settings, "AI_MAX_RECORDED_TOKENS_PER_REQUEST", 10_000_000))
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ValueError("AI usage values must be integers")
        if any(value < 0 or value > max_tokens for value in values) or self.total > max_tokens:
            raise ValueError("AI usage values are outside the accounting bounds")

    @classmethod
    def from_dict(cls, raw: dict) -> Usage:
        if not isinstance(raw, dict):
            raise ValueError("AI usage must be an object")

        def _usage_int(name: str, fallback: str | None = None) -> int:
            value = raw.get(name, raw.get(fallback, 0) if fallback else 0)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("AI usage values must be integers")
            return value

        return cls(
            input_tokens=_usage_int("input_tokens"),
            output_tokens=_usage_int("output_tokens"),
            cache_read_tokens=_usage_int("cache_read_input_tokens", "cache_read_tokens"),
            cache_creation_tokens=_usage_int("cache_creation_input_tokens", "cache_creation_tokens"),
        )

    @property
    def total(self) -> int:
        # Anthropic reports prompt-cache writes and reads separately from base
        # input. They are still billable tokens and must count toward the hard
        # tenant budget; omitting them understates both spend and usage.
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens


def cost_microusd(usage: Usage) -> int:
    """Price every provider token class in integer micro-USD.

    Prompt-cache reads/writes are separate usage classes in Anthropic receipts;
    pricing only base input/output silently under-reports paid usage whenever the
    default provider cache is active.
    """
    inp = settings.AI_COST_PER_MTOK_INPUT_MICROUSD
    out = settings.AI_COST_PER_MTOK_OUTPUT_MICROUSD
    cache_read = settings.AI_COST_PER_MTOK_CACHE_READ_MICROUSD
    cache_write = settings.AI_COST_PER_MTOK_CACHE_WRITE_MICROUSD
    total = (
        (Decimal(usage.input_tokens) / _MTOK) * Decimal(inp)
        + (Decimal(usage.output_tokens) / _MTOK) * Decimal(out)
        + (Decimal(usage.cache_read_tokens) / _MTOK) * Decimal(cache_read)
        + (Decimal(usage.cache_creation_tokens) / _MTOK) * Decimal(cache_write)
    )
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


def budget_snapshot() -> TenantAIBudget:
    """Return effective counters without mutating state on a GET/HEAD.

    Counter rollover is persisted only by a locked budget mutation/reservation.
    A read after a calendar boundary presents zero for the elapsed period on an
    in-memory instance, so browser prefetches and monitoring cannot create the
    singleton row or write rollover state.
    """

    budget = TenantAIBudget.objects.filter(pk=1).first() or TenantAIBudget(pk=1)
    today = timezone.localdate()
    if budget.day_anchor != today:
        budget.tokens_used_today = 0
        budget.day_anchor = today
    if (budget.month_anchor.year, budget.month_anchor.month) != (today.year, today.month):
        budget.tokens_used_month = 0
        budget.month_anchor = today
    return budget


def _apply_request_budget_delta(
    *,
    budget: TenantAIBudget,
    request: AIRequest,
    delta: int,
) -> None:
    """Reconcile only counters that still contain this request's reservation.

    A queued provider job may cross midnight or a month boundary. Rollover removes
    the old period's reservations from the singleton counters; subtracting them
    from the new period would erase unrelated usage and reopen the spend limit.
    Requests are charged to their immutable creation period, matching usage
    reports and making a delayed completion deterministic.
    """

    request_day = timezone.localtime(request.created_at).date()
    dirty: list[str] = []
    if request_day == budget.day_anchor:
        budget.tokens_used_today = max(0, budget.tokens_used_today + delta)
        dirty.append("tokens_used_today")
    if (request_day.year, request_day.month) == (
        budget.month_anchor.year,
        budget.month_anchor.month,
    ):
        budget.tokens_used_month = max(0, budget.tokens_used_month + delta)
        dirty.append("tokens_used_month")
    if dirty:
        budget.save(update_fields=[*dirty, "updated_at"])


def active_prompt(feature: str) -> AIPrompt:
    """The active prompt version for ``feature`` (422 if none seeded)."""
    ensure_ai_enabled()
    try:
        prompt = AIPrompt.objects.get(feature=feature, is_active=True)
    except AIPrompt.DoesNotExist as exc:
        raise ValidationException(
            _("No active AI prompt is configured for this feature."),
            code="ai_prompt_missing",
        ) from exc
    max_output = int(getattr(settings, "AI_MAX_OUTPUT_TOKENS", 16_384))
    max_cost_cap = int(getattr(settings, "AI_MAX_TOKEN_COST_CAP", 1_000_000))
    if (
        prompt.max_output_tokens < 1
        or prompt.max_output_tokens > max_output
        or prompt.token_cost_cap < prompt.max_output_tokens
        or prompt.token_cost_cap > max_cost_cap
        or prompt.effort not in {"low", "medium", "high", "max"}
    ):
        raise ValidationException(
            _("The active AI prompt configuration is invalid."),
            code="ai_prompt_invalid",
        )
    expected_fields = _PROMPT_FIELDS.get(feature)
    max_system_chars = int(getattr(settings, "AI_MAX_SYSTEM_PROMPT_CHARS", 32_000))
    max_template_chars = int(getattr(settings, "AI_MAX_USER_TEMPLATE_CHARS", 32_000))
    try:
        parsed = list(Formatter().parse(prompt.user_template))
    except ValueError as exc:
        raise ValidationException(
            _("The active AI prompt configuration is invalid."),
            code="ai_prompt_invalid",
        ) from exc
    fields = {field for _literal, field, _spec, _conversion in parsed if field is not None}
    has_unsafe_formatting = any(
        field is not None and (bool(spec) or conversion is not None)
        for _literal, field, spec, conversion in parsed
    )
    if (
        expected_fields is None
        or fields != expected_fields
        or has_unsafe_formatting
        or not prompt.system_prompt
        or len(prompt.system_prompt) > max_system_chars
        or not prompt.user_template
        or len(prompt.user_template) > max_template_chars
    ):
        raise ValidationException(
            _("The active AI prompt configuration is invalid."),
            code="ai_prompt_invalid",
        )
    return prompt


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


def _request_is_redrivable(request: AIRequest) -> bool:
    # A failed row is safe to replay only when execution stopped before the
    # irreversible provider-attempt marker. A completed/ambiguous paid attempt
    # must reuse its stored receipt or remain closed for manual reconciliation.
    return request.status == AIRequest.Status.DENIED_BUDGET or (
        request.status == AIRequest.Status.FAILED and not request.provider_attempt_id
    )


def check_and_reserve_budget(
    *,
    feature: str,
    estimated_tokens: int | None = None,
    requested_by=None,
    requested_by_id: int | None = None,
    requested_principal: RolePrincipal | None = None,
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
    ensure_ai_enabled()
    from apps.ai.authorization import (
        assert_principal_authorizes_source,
        feature_parameter_fingerprint,
        resolve_request_principal,
        resolve_source_authorization,
    )

    prompt = active_prompt(feature)
    if requested_by is None and requested_by_id is not None:
        from apps.users.models import User

        requested_by = User.objects.filter(pk=requested_by_id, is_active=True).first()
    principal = resolve_request_principal(
        requested_by=requested_by,
        requested_principal=requested_principal,
        feature=feature,
        source_id=source_id,
    )
    source = resolve_source_authorization(
        feature=feature,
        source_app=source_app,
        source_id=source_id,
        principal_kind=principal.kind,
    )
    assert_principal_authorizes_source(user=requested_by, principal=principal, source=source)
    params_fingerprint = feature_parameter_fingerprint(feature=feature, params=params)
    key = make_idempotency_key(
        feature=feature, source_app=source_app, source_id=source_id, version=prompt.version, params=params
    )
    actor_id = principal.user_id
    if estimated_tokens is None:
        # Production callers reserve against the exact prompt version selected
        # above. Looking the prompt up in the caller and again here introduced a
        # race where the active version (and its cap) could change between reads.
        requested = prompt.token_cost_cap
    else:
        if isinstance(estimated_tokens, bool):
            raise ValidationException(_("Invalid token reservation."), code="invalid_ai_token_reservation")
        try:
            requested = int(estimated_tokens)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationException(
                _("Invalid token reservation."), code="invalid_ai_token_reservation"
            ) from exc
    if requested < 1 or requested > prompt.token_cost_cap:
        raise ValidationException(_("Invalid token reservation."), code="invalid_ai_token_reservation")

    def _create(status: str, *, reserved: int = 0) -> tuple[AIRequest, bool]:
        # get_or_create on the unique key makes a concurrent duplicate a no-op.
        return AIRequest.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "feature": feature,
                "status": status,
                "prompt": prompt,
                "requested_by_id": actor_id,
                "requested_principal_kind": principal.kind,
                "requested_principal_id": principal.principal_id,
                "attribution_status": AIRequest.AttributionStatus.RESOLVED,
                "scope_status": source.scope_status,
                "branch_at_request_id": source.branch_id,
                "department_at_request_id": source.department_id,
                "authorization_permission": source.permission,
                "source_app": source_app,
                "source_id": source_id,
                "parameter_fingerprint": params_fingerprint,
                "reserved_tokens": reserved,
            },
        )

    def _assert_same_intent(existing: AIRequest) -> None:
        expected = (
            existing.feature == feature,
            existing.prompt_id == prompt.pk,
            existing.requested_by_id == actor_id,
            existing.requested_principal_kind == principal.kind,
            existing.requested_principal_id == principal.principal_id,
            existing.attribution_status == AIRequest.AttributionStatus.RESOLVED,
            existing.scope_status == source.scope_status,
            existing.branch_at_request_id == source.branch_id,
            existing.department_at_request_id == source.department_id,
            existing.authorization_permission == source.permission,
            existing.source_app == source_app,
            existing.source_id == source_id,
            existing.parameter_fingerprint == params_fingerprint,
        )
        if not all(expected):
            raise ConflictException(
                _("The existing AI request has a different authorization context."),
                code="ai_request_context_conflict",
            )

    with transaction.atomic():
        # Lock the budget first so the existing-row read + any reservation are
        # serialized (a concurrent duplicate / re-drive can't double-spend).
        budget = _get_budget_locked()
        existing = AIRequest.objects.select_for_update().filter(idempotency_key=key).first()
        if existing is not None:
            _assert_same_intent(existing)
        if existing is not None and not _request_is_redrivable(existing):
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
                existing.celery_task_id = ""
                existing.started_at = None
                existing.finished_at = None
                existing.save(
                    update_fields=[
                        "status",
                        "reserved_tokens",
                        "error_detail",
                        "celery_task_id",
                        "started_at",
                        "finished_at",
                    ]
                )
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
        denied, created = _create(AIRequest.Status.DENIED_BUDGET)
        _assert_same_intent(denied)
        # A concurrent request may have re-driven the same intent after the
        # budget lock above was released. Do not report a false denial or clobber
        # that in-flight/succeeded state.
        if not created and not _request_is_redrivable(denied):
            denied._should_enqueue = False
            return denied
        if not created:
            denied.status = AIRequest.Status.DENIED_BUDGET
            denied.reserved_tokens = 0
            denied.error_detail = "budget_denied"
            denied.celery_task_id = ""
            denied.started_at = None
            denied.finished_at = timezone.now()
            denied.save(
                update_fields=[
                    "status",
                    "reserved_tokens",
                    "error_detail",
                    "celery_task_id",
                    "started_at",
                    "finished_at",
                ]
            )
    if disabled:
        raise AIBudgetExceeded(_("AI is disabled for this center."), code="ai_budget_exceeded")
    raise AIBudgetExceeded(
        _("The AI token budget for this period has been exhausted."),
        code="ai_budget_exceeded",
    )


@transaction.atomic
def record_usage(*, ai_request_id: int, usage: Usage, billable: bool = True) -> None:
    """Finish a receipt-less completion and reconcile it to the tenant budget.

    The request reserved ``reserved_tokens`` (the estimate) at queue time; here we
    move the budget by the *delta* (actual - reserved) and zero the reservation,
    so the net effect of reserve+reconcile equals the real usage. Guarded by
    status so a retried task never double-reconciles.

    ``billable=False`` (a Redis response-cache hit — no tokens were actually
    purchased) records the usage columns for transparency but bills ZERO: the
    reservation is fully released and ``cost_microusd`` stays 0.
    """
    # Canonical lock order is budget -> request, matching reservation.  Reversing
    # it lets a duplicate enqueue deadlock a completion (budget→request vs
    # request→budget).
    budget = _get_budget_locked()
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
    # A receipt-less request cannot remain RUNNING after releasing its entire
    # reservation: that state is neither safely retryable nor reconcilable.
    # Paid provider work uses record_provider_completion(), which persists the
    # receipt and may remain RUNNING while downstream output is applied.
    req.status = AIRequest.Status.SUCCEEDED
    req.finished_at = timezone.now()
    req.save(
        update_fields=[
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cost_microusd",
            "reserved_tokens",
            "status",
            "finished_at",
        ]
    )

    # Reconcile under the lock: replace the reserved estimate with the billed
    # amount. Clamp at 0 so a day/month rollover between reserve and reconcile
    # can't drive the counter negative.
    delta = billed - reserved
    _apply_request_budget_delta(budget=budget, request=req, delta=delta)


@transaction.atomic
def record_provider_completion(
    *,
    ai_request_id: int,
    usage: Usage,
    output: str,
    provider_request_id: str,
    provider_stop_reason: str,
) -> AIRequest:
    """Persist the paid-provider receipt before applying model output downstream.

    A worker crash after this commit reuses the encrypted result instead of buying
    another completion.  Usage reconciliation remains idempotent because
    ``record_usage`` only accepts queued/running rows with a non-zero reservation.
    """

    budget = _get_budget_locked()
    req = AIRequest.objects.select_for_update().get(pk=ai_request_id)
    if req.status not in (AIRequest.Status.QUEUED, AIRequest.Status.RUNNING):
        return req
    if req.provider_request_id:
        return req
    if not req.provider_attempt_id or req.provider_attempted_at is None:
        raise ValueError("AI provider completion has no durable attempt marker")
    max_chars = int(getattr(settings, "AI_MAX_STORED_OUTPUT_CHARS", 250_000))
    if not isinstance(output, str) or len(output) > max_chars:
        raise ValueError("AI output is outside the storage bounds")
    if not isinstance(provider_request_id, str) or not provider_request_id or len(provider_request_id) > 255:
        raise ValueError("AI provider receipt is invalid")
    if provider_stop_reason not in _PROVIDER_STOP_REASONS:
        raise ValueError("AI provider stop reason is invalid")
    # Inline the accounting reconciliation under this transaction/row lock so
    # output and its cost receipt cannot diverge.
    reserved = req.reserved_tokens
    req.input_tokens = usage.input_tokens
    req.output_tokens = usage.output_tokens
    req.cache_read_tokens = usage.cache_read_tokens
    req.cache_creation_tokens = usage.cache_creation_tokens
    req.cost_microusd = cost_microusd(usage)
    req.reserved_tokens = 0
    req.output_ciphertext = output
    req.provider_request_id = provider_request_id
    req.provider_stop_reason = provider_stop_reason
    req.redaction_map = ""
    req.save(
        update_fields=[
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cost_microusd",
            "reserved_tokens",
            "output_ciphertext",
            "provider_request_id",
            "provider_stop_reason",
            "redaction_map",
        ]
    )
    delta = usage.total - reserved
    _apply_request_budget_delta(budget=budget, request=req, delta=delta)
    return req


@transaction.atomic
def begin_provider_attempt(*, ai_request_id: int, task_id: str) -> AIRequest:
    """Persist the irreversible external-call boundary before any network I/O.

    Anthropic's messages API does not provide an application idempotency key in
    this integration. Once this marker commits, an absent provider receipt is an
    ambiguous paid outcome and automatic retries are forbidden.
    """

    if not isinstance(task_id, str) or not task_id or len(task_id) > 64:
        raise ValueError("AI task identity is invalid")
    request = AIRequest.objects.select_for_update().get(pk=ai_request_id)
    if request.status != AIRequest.Status.RUNNING or request.celery_task_id != task_id:
        return request
    if request.provider_attempt_id:
        return request
    request.provider_attempt_id = uuid4().hex
    request.provider_attempted_at = timezone.now()
    request.save(update_fields=["provider_attempt_id", "provider_attempted_at"])
    return request


@transaction.atomic
def quarantine_ambiguous_provider_attempt(*, ai_request_id: int) -> AIRequest | None:
    """Fail closed when a started provider call has no durable completion.

    The original reservation remains charged conservatively because the real
    provider usage is unknowable. Operations must reconcile it manually; a new
    automatic model call would risk duplicate spend and duplicate output.
    """

    request = AIRequest.objects.select_for_update().filter(pk=ai_request_id).first()
    if request is None:
        return None
    if request.provider_request_id or request.output_ciphertext:
        return request
    if not request.provider_attempt_id:
        return request
    if request.status == AIRequest.Status.UNCERTAIN:
        return request
    if request.reserved_tokens <= 0:
        raise ValueError("Ambiguous AI provider attempt has no conservative reservation")
    request.status = AIRequest.Status.UNCERTAIN
    request.error_detail = "provider_outcome_unknown"
    request.redaction_map = ""
    request.output_ciphertext = ""
    request.content_purged_at = timezone.now()
    request.finished_at = timezone.now()
    request.save(
        update_fields=[
            "status",
            "error_detail",
            "redaction_map",
            "output_ciphertext",
            "content_purged_at",
            "finished_at",
        ]
    )
    return request


@transaction.atomic
def reconcile_ambiguous_provider_attempt(
    *,
    ai_request_id: int,
    outcome: str,
    reference: str,
    usage: Usage | None = None,
    provider_request_id: str = "",
    provider_stop_reason: str = "",
) -> AIRequest:
    """Resolve one quarantined provider attempt from reviewed provider evidence.

    ``not_charged`` releases the conservative reservation. ``charged`` requires
    the real provider receipt and all token classes, replacing the reservation
    with audited actual usage. The generated response is unavailable after an
    ambiguous crash, so either outcome closes the request as failed; neither can
    trigger another model call.
    """

    if outcome not in {"not_charged", "charged"}:
        raise ValueError("AI provider reconciliation outcome is invalid")
    if (
        not isinstance(reference, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", reference) is None
    ):
        raise ValueError("AI provider reconciliation reference is invalid")
    if outcome == "charged":
        if not isinstance(usage, Usage):
            raise ValueError("Charged AI reconciliation requires validated usage")
        if (
            not isinstance(provider_request_id, str)
            or not provider_request_id
            or len(provider_request_id) > 255
            or provider_stop_reason not in _PROVIDER_STOP_REASONS
        ):
            raise ValueError("Charged AI reconciliation requires a valid provider receipt")
    elif usage is not None or provider_request_id or provider_stop_reason:
        raise ValueError("Not-charged AI reconciliation cannot carry a provider receipt")

    budget = _get_budget_locked()
    request = AIRequest.objects.select_for_update().get(pk=ai_request_id)
    if request.provider_reconciliation_status:
        if (
            request.provider_reconciliation_status == outcome
            and request.provider_reconciliation_reference == reference
        ):
            return request
        raise ConflictException(
            _("The AI provider attempt was already reconciled."),
            code="ai_provider_already_reconciled",
        )
    if (
        request.status != AIRequest.Status.UNCERTAIN
        or not request.provider_attempt_id
        or request.provider_request_id
        or request.reserved_tokens <= 0
    ):
        raise ConflictException(
            _("The AI provider attempt is not awaiting reconciliation."),
            code="ai_provider_not_uncertain",
        )

    reserved = request.reserved_tokens
    request.status = AIRequest.Status.FAILED
    request.reserved_tokens = 0
    request.provider_reconciliation_status = outcome
    request.provider_reconciliation_reference = reference
    request.provider_reconciled_at = timezone.now()
    request.error_detail = f"provider_reconciled_{outcome}"
    request.redaction_map = ""
    request.output_ciphertext = ""
    request.content_purged_at = request.content_purged_at or timezone.now()
    request.finished_at = timezone.now()
    update_fields = [
        "status",
        "reserved_tokens",
        "provider_reconciliation_status",
        "provider_reconciliation_reference",
        "provider_reconciled_at",
        "error_detail",
        "redaction_map",
        "output_ciphertext",
        "content_purged_at",
        "finished_at",
    ]
    billed = 0
    if outcome == "charged":
        assert usage is not None  # validated above
        request.input_tokens = usage.input_tokens
        request.output_tokens = usage.output_tokens
        request.cache_read_tokens = usage.cache_read_tokens
        request.cache_creation_tokens = usage.cache_creation_tokens
        request.cost_microusd = cost_microusd(usage)
        request.provider_request_id = provider_request_id
        request.provider_stop_reason = provider_stop_reason
        billed = usage.total
        update_fields.extend(
            [
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "cost_microusd",
                "provider_request_id",
                "provider_stop_reason",
            ]
        )
    request.save(update_fields=update_fields)
    _apply_request_budget_delta(
        budget=budget,
        request=request,
        delta=billed - reserved,
    )
    return request


@transaction.atomic
def release_reservation(*, ai_request_id: int) -> None:
    """Return a request's outstanding reservation to the budget (terminal failure).

    Idempotent: a request that already reconciled (record_usage zeroed
    ``reserved_tokens``) releases nothing. Clamps at 0 against a rollover."""
    budget = _get_budget_locked()
    req = AIRequest.objects.select_for_update().get(pk=ai_request_id)
    reserved = req.reserved_tokens
    if reserved <= 0:
        return
    req.reserved_tokens = 0
    req.save(update_fields=["reserved_tokens"])
    _apply_request_budget_delta(budget=budget, request=req, delta=-reserved)


@transaction.atomic
def terminalize_failure(*, ai_request_id: int, error_code: str) -> AIRequest | None:
    """Atomically fail non-terminal work, release its reservation, and purge content."""

    if not isinstance(error_code, str) or not error_code or len(error_code) > 64:
        raise ValueError("AI failure code is invalid")
    budget = _get_budget_locked()
    req = AIRequest.objects.select_for_update().filter(pk=ai_request_id).first()
    if req is None:
        return None
    if req.status in (
        AIRequest.Status.SUCCEEDED,
        AIRequest.Status.DENIED_BUDGET,
        AIRequest.Status.UNCERTAIN,
    ):
        return req
    reserved = req.reserved_tokens
    req.status = AIRequest.Status.FAILED
    req.reserved_tokens = 0
    req.error_detail = error_code
    req.redaction_map = ""
    req.output_ciphertext = ""
    req.content_purged_at = timezone.now()
    req.finished_at = timezone.now()
    req.save(
        update_fields=[
            "status",
            "reserved_tokens",
            "error_detail",
            "redaction_map",
            "output_ciphertext",
            "content_purged_at",
            "finished_at",
        ]
    )
    if reserved:
        _apply_request_budget_delta(budget=budget, request=req, delta=-reserved)
    return req


@transaction.atomic
def update_budget(*, daily_token_limit=None, monthly_token_limit=None, is_enabled=None) -> TenantAIBudget:
    """Director-only mutation of the budget limits / enabled flag (D4-LA-8)."""
    budget = _get_budget_locked()
    fields: list[str] = []
    max_limit = int(getattr(settings, "AI_MAX_BUDGET_TOKENS", 2_000_000_000))
    if daily_token_limit is not None:
        if isinstance(daily_token_limit, bool) or not 0 <= int(daily_token_limit) <= max_limit:
            raise ValidationException(_("Invalid daily token limit."), code="invalid_ai_budget")
        budget.daily_token_limit = int(daily_token_limit)
        fields.append("daily_token_limit")
    if monthly_token_limit is not None:
        if isinstance(monthly_token_limit, bool) or not 0 <= int(monthly_token_limit) <= max_limit:
            raise ValidationException(_("Invalid monthly token limit."), code="invalid_ai_budget")
        budget.monthly_token_limit = int(monthly_token_limit)
        fields.append("monthly_token_limit")
    if is_enabled is not None:
        budget.is_enabled = bool(is_enabled)
        fields.append("is_enabled")
    if budget.monthly_token_limit < budget.daily_token_limit:
        raise ValidationException(
            _("The monthly token limit cannot be lower than the daily limit."),
            code="invalid_ai_budget",
            fields={"monthly_token_limit": [_("Must be greater than or equal to the daily limit.")]},
        )
    if fields:
        try:
            budget.full_clean()
        except DjangoValidationError as exc:
            raise ValidationException(
                _("Invalid AI budget."), code="invalid_ai_budget", fields=exc.message_dict
            ) from exc
        budget.save(update_fields=[*fields, "updated_at"])
    return budget


def request_exam_generation(
    *,
    subject_id: int,
    exam_type: str,
    question_count: int,
    difficulty: str,
    requested_by=None,
    requested_principal: RolePrincipal | None = None,
) -> AIRequest:
    """Request-driven exam generation (D4-LA-8). Gated by
    ``CenterSettings.ai_exam_generation_enabled`` (TD-13), then budget-reserved
    and enqueued on commit. The Subject id is the source row for idempotency.

    NOT ``@transaction.atomic`` at this level: ``check_and_reserve_budget`` owns
    its own transactions so the ``denied_budget`` row survives a raised
    ``AIBudgetExceeded`` (an enclosing atomic here would roll it back)."""
    from apps.ai.authorization import validate_exam_generation_parameters
    from apps.org.selectors import get_center_settings

    if not get_center_settings().ai_exam_generation_enabled:
        raise AIFeatureDisabled(code="feature_disabled")
    validate_exam_generation_parameters(
        subject_id=subject_id,
        exam_type=exam_type,
        question_count=question_count,
        difficulty=difficulty,
    )

    ai_request = check_and_reserve_budget(
        feature=AIFeature.EXAM_GENERATION,
        requested_by=requested_by,
        requested_principal=requested_principal,
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
