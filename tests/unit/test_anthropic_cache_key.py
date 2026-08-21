"""Anthropic response-cache key unit tests (D1-LA-4 / TD-17). No DB, no network.

max_tokens and effort change the response, so two calls differing only in one
of them must never share a Redis cache key.
"""

from infrastructure.ai.anthropic_client import _cache_key


def _key(*, max_tokens: int, effort: str) -> str:
    return _cache_key(
        model="test-model",
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=max_tokens,
        effort=effort,
    )


def test_cache_key_differs_when_only_max_tokens_differs():
    assert _key(max_tokens=1000, effort="high") != _key(max_tokens=2000, effort="high")


def test_cache_key_differs_when_only_effort_differs():
    assert _key(max_tokens=1000, effort="high") != _key(max_tokens=1000, effort="low")


def test_cache_key_stable_for_identical_inputs():
    assert _key(max_tokens=1000, effort="high") == _key(max_tokens=1000, effort="high")
