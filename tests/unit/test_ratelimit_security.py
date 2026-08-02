from __future__ import annotations

from unittest.mock import Mock

import pytest

from core.exceptions import ServiceUnavailableException, ThrottledException
from core.ratelimit import _consume


def test_rate_limit_cache_key_does_not_retain_plain_identifier(monkeypatch):
    cache = Mock()
    cache.add.return_value = True
    monkeypatch.setattr("core.ratelimit.cache", cache)

    identifier = "executive@example.test"
    _consume("login_user", identifier, limit=5, window=60)

    bucket = cache.add.call_args.args[0]
    assert bucket.startswith("rl:login_user:")
    assert identifier not in bucket
    assert "executive" not in bucket
    assert len(bucket.removeprefix("rl:login_user:")) == 20


def test_rate_limit_still_rejects_requests_above_the_cap(monkeypatch):
    cache = Mock()
    cache.add.return_value = False
    cache.incr.return_value = 6
    monkeypatch.setattr("core.ratelimit.cache", cache)

    with pytest.raises(ThrottledException) as exc_info:
        _consume("login_user", "executive@example.test", limit=5, window=60)

    assert exc_info.value.code == "throttled"
    assert exc_info.value.wait == 60


def test_rate_limit_backend_outage_fails_closed_without_identifier_in_log(monkeypatch, caplog):
    cache = Mock()
    cache.add.side_effect = ConnectionError("redis unavailable")
    monkeypatch.setattr("core.ratelimit.cache", cache)

    identifier = "198.51.100.24"
    with pytest.raises(ServiceUnavailableException) as exc_info:
        _consume("login_ip", identifier, limit=10, window=60)

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "temporarily_unavailable"
    assert identifier not in caplog.text


def test_rate_limit_expiry_race_restarts_the_window(monkeypatch):
    cache = Mock()
    cache.add.return_value = False
    cache.incr.side_effect = ValueError("expired")
    monkeypatch.setattr("core.ratelimit.cache", cache)

    _consume("login_ip", "198.51.100.24", limit=10, window=60)

    cache.set.assert_called_once()
    assert cache.set.call_args.kwargs["timeout"] == 60
