"""Anthropic Claude client wrapper.

Defaults:
- Model: `settings.ANTHROPIC_DEFAULT_MODEL` (one source of truth; `claude-sonnet-4-6`).
- Adaptive thinking with effort tunable per call.
- Top-level prompt caching (`cache_control: {"type": "ephemeral"}`)
  enabled by default — caches `tools` + `system` automatically.
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
    use_response_cache: bool = True,
) -> dict[str, Any]:
    """Send a message to Claude and return `{text, usage, raw}`.

    - `use_cache`: enables Anthropic's prompt cache (cheap; cost is paid at write).
    - `use_response_cache`: short-circuits identical prompts via Redis. Default on.
      Disable when temperature-equivalent variation is desirable (rare for Edu).
    """

    model = model or settings.ANTHROPIC_DEFAULT_MODEL
    redis_key = _cache_key(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        effort=effort,
    )

    if use_response_cache:
        cached = cache.get(redis_key)
        if cached is not None:
            # Flag the hit so callers (apps/ai record_usage) bill ZERO tokens — a
            # cached response purchased nothing from the API this time (TD-17).
            return {**cached, "cache_hit": True}

    if settings.ANTHROPIC_USE_MOCK:
        result = _mock_complete(
            model=model, system=system, messages=messages, max_tokens=max_tokens, effort=effort
        )
        if use_response_cache:
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
    text = next((b.text for b in response.content if b.type == "text"), "")
    result = {
        "text": text,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0),
        },
        "stop_reason": response.stop_reason,
        "raw_id": response.id,
    }
    if use_response_cache:
        cache.set(redis_key, result, timeout=settings.ANTHROPIC_PROMPT_CACHE_TTL_SECONDS)
    return result
