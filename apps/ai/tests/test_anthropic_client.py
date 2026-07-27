"""Anthropic client mock + TD-17 cache-key regression (D4-LA-2/3)."""

from __future__ import annotations

import pytest
from django.test import override_settings
from django_tenants.utils import schema_context

from infrastructure.ai import anthropic_client
from infrastructure.ai.anthropic_client import _cache_key, complete

pytestmark = pytest.mark.django_db

_MSGS = [{"role": "user", "content": "Summarize the lesson."}]


def test_cache_key_includes_max_tokens():
    """TD-17: two calls differing only in max_tokens MUST produce different keys."""
    with schema_context("tenant_a"):
        k1 = _cache_key(model="m", system="s", messages=_MSGS, max_tokens=100, effort="high")
        k2 = _cache_key(model="m", system="s", messages=_MSGS, max_tokens=200, effort="high")
    assert k1 != k2


def test_cache_key_includes_effort():
    """TD-17: two calls differing only in effort MUST produce different keys."""
    with schema_context("tenant_a"):
        k1 = _cache_key(model="m", system="s", messages=_MSGS, max_tokens=100, effort="low")
        k2 = _cache_key(model="m", system="s", messages=_MSGS, max_tokens=100, effort="high")
    assert k1 != k2


def test_cache_key_is_schema_scoped():
    with schema_context("tenant_a"):
        ka = _cache_key(model="m", system="s", messages=_MSGS, max_tokens=100, effort="high")
    with schema_context("tenant_b"):
        kb = _cache_key(model="m", system="s", messages=_MSGS, max_tokens=100, effort="high")
    assert ka != kb


@override_settings(ANTHROPIC_USE_MOCK=True)
def test_mock_is_deterministic_and_makes_no_http(monkeypatch):
    # If the real client were ever constructed, fail loudly.
    monkeypatch.setattr(
        anthropic_client,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("real client used under mock")),
    )
    with schema_context("tenant_a"):
        r1 = complete(messages=_MSGS, system="s", max_tokens=512, effort="medium", use_response_cache=False)
        r2 = complete(messages=_MSGS, system="s", max_tokens=512, effort="medium", use_response_cache=False)
    assert r1["text"] == r2["text"]
    assert r1["usage"] == r2["usage"]
    assert r1.get("mock") is True
    assert r1["usage"]["input_tokens"] > 0


@override_settings(ANTHROPIC_USE_MOCK=True)
def test_mock_output_respects_max_tokens():
    with schema_context("tenant_a"):
        r = complete(messages=_MSGS, system="s", max_tokens=10, effort="medium", use_response_cache=False)
    assert r["usage"]["output_tokens"] <= 10
