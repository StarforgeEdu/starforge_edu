"""Anthropic Claude client wrapper.

Defaults:
- Model: `settings.ANTHROPIC_DEFAULT_MODEL` (one source of truth; `claude-sonnet-4-6`).
- Adaptive thinking with effort tunable per call.
- Top-level provider prompt caching (`cache_control: {"type": "ephemeral"}`)
  is enabled by default for reviewed system material.
- Application response caching is disabled by default; enabling it is an
  explicit caller decision because a cache hit has different cost semantics.
- Streaming optional; use for any call with large `max_tokens`.

This wrapper is intentionally thin. Real prompts/tools/orchestration
belong in apps/ai/ (Celery-only — see TenantAIBudget enforcement).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.core.cache import cache

from core.utils import current_schema, stable_hash


class InvalidAIProviderResponse(RuntimeError):
    """The paid provider returned data outside the reviewed protocol."""


def _bounded_int(value: Any, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise InvalidAIProviderResponse(f"Invalid AI provider {name}.")
    return value


def _validate_call(
    *,
    model: str,
    system: str | None,
    messages: list[dict[str, Any]],
    max_tokens: int,
    effort: str,
) -> None:
    configured_models = getattr(
        settings,
        "ANTHROPIC_ALLOWED_MODELS",
        (settings.ANTHROPIC_DEFAULT_MODEL,),
    )
    allowed_models = {configured_models} if isinstance(configured_models, str) else set(configured_models)
    if not isinstance(model, str) or not model or model not in allowed_models:
        raise ValueError("AI model is not allowlisted")
    max_output_tokens = int(getattr(settings, "AI_MAX_OUTPUT_TOKENS", 16_384))
    if (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or not 1 <= max_tokens <= max_output_tokens
    ):
        raise ValueError("AI max_tokens is outside the configured bound")
    if effort not in {"low", "medium", "high", "max"}:
        raise ValueError("AI effort is not supported")
    max_system_chars = int(getattr(settings, "AI_MAX_SYSTEM_PROMPT_CHARS", 32_000))
    if system is not None and (not isinstance(system, str) or len(system) > max_system_chars):
        raise ValueError("AI system prompt is outside the configured bound")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 16:
        raise ValueError("AI messages are outside the configured bound")
    total_chars = len(system or "")
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ValueError("AI message shape is invalid")
        if message["role"] not in {"user", "assistant"} or not isinstance(message["content"], str):
            raise ValueError("AI message content is invalid")
        total_chars += len(message["content"])
    if total_chars > int(getattr(settings, "AI_MAX_INPUT_CHARS", 128_000)):
        raise ValueError("AI input is outside the configured bound")


def validate_completion_request(
    *,
    messages: list[dict[str, Any]],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 16000,
    effort: str = "high",
) -> None:
    """Validate locally before a worker commits its external-attempt marker."""

    _validate_call(
        model=model or settings.ANTHROPIC_DEFAULT_MODEL,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        effort=effort,
    )


def count_input_tokens(
    *,
    messages: list[dict[str, Any]],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 16000,
    effort: str = "high",
    use_cache: bool = True,
) -> int:
    """Count provider input before crossing the paid-completion boundary.

    The tenant budget reserves a prompt version's total token cap. A character
    limit alone cannot prove that adversarial Unicode or a long submission fits
    that cap, so production workers use the provider's token-count endpoint and
    reject the request before buying a completion when input plus maximum output
    would exceed the reservation.
    """

    model = model or settings.ANTHROPIC_DEFAULT_MODEL
    _validate_call(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        effort=effort,
    )
    max_usage = int(getattr(settings, "AI_MAX_RECORDED_TOKENS_PER_REQUEST", 10_000_000))
    if settings.ANTHROPIC_USE_MOCK:
        payload = json.dumps(
            {
                "model": model,
                "system": system,
                "messages": messages,
                "thinking": "adaptive",
                "effort": effort,
            },
            sort_keys=True,
        )
        return _bounded_int(
            max(1, len(payload.encode("utf-8")) // 4),
            name="input token count",
            maximum=max_usage,
        )

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if system is not None:
        kwargs["system"] = system
    if use_cache:
        kwargs["cache_control"] = {"type": "ephemeral"}
    response = get_client().messages.count_tokens(**kwargs)
    return _bounded_int(
        getattr(response, "input_tokens", None),
        name="input token count",
        maximum=max_usage,
    )


def _validate_result(result: Any, *, max_tokens: int) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise InvalidAIProviderResponse("Invalid AI provider response.")
    text = result.get("text")
    raw_id = result.get("raw_id")
    stop_reason = result.get("stop_reason")
    usage = result.get("usage")
    max_chars = int(getattr(settings, "AI_MAX_STORED_OUTPUT_CHARS", 250_000))
    if not isinstance(text, str) or len(text) > max_chars:
        raise InvalidAIProviderResponse("Invalid AI provider output.")
    if not isinstance(raw_id, str) or not raw_id or len(raw_id) > 255:
        raise InvalidAIProviderResponse("Invalid AI provider receipt.")
    if stop_reason not in {"end_turn", "max_tokens", "stop_sequence", "refusal"}:
        # ``tool_use`` and ``pause_turn`` are intentionally not accepted.  This
        # application has no reviewed model tools or external retrieval loop.
        raise InvalidAIProviderResponse("Unsupported AI provider stop reason.")
    if not isinstance(usage, dict):
        raise InvalidAIProviderResponse("Invalid AI provider usage.")
    max_usage = int(getattr(settings, "AI_MAX_RECORDED_TOKENS_PER_REQUEST", 10_000_000))
    normalized_usage = {
        "input_tokens": _bounded_int(usage.get("input_tokens"), name="input usage", maximum=max_usage),
        "output_tokens": _bounded_int(usage.get("output_tokens"), name="output usage", maximum=max_tokens),
        "cache_read_input_tokens": _bounded_int(
            usage.get("cache_read_input_tokens", 0), name="cache-read usage", maximum=max_usage
        ),
        "cache_creation_input_tokens": _bounded_int(
            usage.get("cache_creation_input_tokens", 0),
            name="cache-creation usage",
            maximum=max_usage,
        ),
    }
    if sum(normalized_usage.values()) > max_usage:
        raise InvalidAIProviderResponse("Invalid AI provider total usage.")
    validated = {
        "text": text,
        "usage": normalized_usage,
        "stop_reason": stop_reason,
        "raw_id": raw_id,
    }
    if result.get("mock") is True:
        validated["mock"] = True
    return validated


@lru_cache(maxsize=1)
def get_client():  # pragma: no cover - real client never constructed under mock
    # Imported lazily so the SDK is only required when a real (non-mock) call is
    # made. With ANTHROPIC_USE_MOCK on (the default outside production, TD-2),
    # `complete()` never reaches here and `anthropic` need not be importable.
    import anthropic

    # Explicit timeout so a stuck HTTP call fails fast (and the Celery task can
    # retry) instead of hanging toward the task soft time limit. Sized well under
    # CELERY_TASK_SOFT_TIME_LIMIT (25 min).
    timeout = getattr(settings, "ANTHROPIC_REQUEST_TIMEOUT_SECONDS", 120.0)
    return anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=timeout)


def _mock_complete(
    *,
    model: str,
    system: str | None,
    messages: list[dict[str, Any]],
    max_tokens: int,
    effort: str,
) -> dict[str, Any]:
    """Deterministic, zero-HTTP stand-in for the Anthropic API (TD-2, D4-LA-2).

    Same inputs -> identical ``{text, usage}``. Usage is derived from the prompt
    size so budget accounting and cost math exercise realistic numbers without a
    network call. Flip ``ANTHROPIC_USE_MOCK=False`` (production) for the real API.
    """
    payload = json.dumps(
        {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens, "effort": effort},
        sort_keys=True,
    )
    digest = stable_hash(payload)
    # ~4 chars/token heuristic, deterministic from the prompt; output capped at
    # max_tokens so the mock respects the per-prompt ceiling.
    prompt_chars = len(payload)
    input_tokens = max(1, prompt_chars // 4)
    output_tokens = min(max_tokens, 64 + (int(digest[:8], 16) % 256))
    return {
        "text": f"[MOCK-AI:{model}:{digest[:12]}] Deterministic completion for testing.",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        "stop_reason": "end_turn",
        "raw_id": f"mock_{digest[:24]}",
        "mock": True,
    }


def _cache_key(
    *,
    model: str,
    system: str | None,
    messages: Iterable[dict[str, Any]],
    max_tokens: int,
    effort: str,
) -> str:
    # max_tokens and effort change the response, so they MUST be part of the
    # cache key — otherwise two calls that differ only in those parameters
    # would collide and serve a wrong cached response (TD-17).
    payload = json.dumps(
        {
            "model": model,
            "system": system,
            "messages": list(messages),
            "max_tokens": max_tokens,
            "effort": effort,
        },
        sort_keys=True,
    )
    # Schema-scoped: the Redis cache is shared across tenants, so an unscoped key
    # would serve tenant A's AI response to tenant B on a byte-identical prompt.
    return f"anthropic:resp:{current_schema()}:{stable_hash(payload)}"


def complete(
    *,
    messages: list[dict[str, Any]],
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 16000,
    effort: str = "high",
    use_cache: bool = True,
    use_response_cache: bool = False,
) -> dict[str, Any]:
    """Send a message to Claude and return `{text, usage, raw}`.

    - `use_cache`: enables Anthropic's prompt cache (cheap; cost is paid at write).
    - `use_response_cache`: short-circuits identical prompts via Redis. Default
      off; callers that opt in must account for cache-hit billing semantics.
    """

    model = model or settings.ANTHROPIC_DEFAULT_MODEL
    _validate_call(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        effort=effort,
    )
    redis_key = (
        _cache_key(
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            effort=effort,
        )
        if use_response_cache
        else None
    )

    if use_response_cache:
        assert redis_key is not None
        cached = cache.get(redis_key)
        if cached is not None:
            try:
                validated = _validate_result(cached, max_tokens=max_tokens)
            except InvalidAIProviderResponse:
                cache.delete(redis_key)
            else:
                return {**validated, "cache_hit": True}

    if settings.ANTHROPIC_USE_MOCK:
        result = _validate_result(
            _mock_complete(
                model=model, system=system, messages=messages, max_tokens=max_tokens, effort=effort
            ),
            max_tokens=max_tokens,
        )
        if use_response_cache:
            assert redis_key is not None
            cache.set(redis_key, result, timeout=settings.ANTHROPIC_PROMPT_CACHE_TTL_SECONDS)
        return result

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    if system is not None:
        kwargs["system"] = system
    if use_cache:
        kwargs["cache_control"] = {"type": "ephemeral"}

    response = get_client().messages.create(**kwargs)
    blocks = list(response.content)
    if len(blocks) > 64 or any(
        getattr(block, "type", "") not in {"text", "thinking", "redacted_thinking"} for block in blocks
    ):
        raise InvalidAIProviderResponse("Unsupported AI provider content block.")
    text = "".join(
        block.text for block in blocks if getattr(block, "type", "") == "text" and isinstance(block.text, str)
    )
    result = _validate_result(
        {
            "text": text,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
                "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
            },
            "stop_reason": response.stop_reason,
            "raw_id": response.id,
        },
        max_tokens=max_tokens,
    )
    if use_response_cache:
        assert redis_key is not None
        cache.set(redis_key, result, timeout=settings.ANTHROPIC_PROMPT_CACHE_TTL_SECONDS)
    return result
