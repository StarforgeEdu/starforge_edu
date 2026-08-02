"""Strict parsing for operator-supplied fixed-window rate limits."""

from __future__ import annotations

import re

_RATE_PATTERN = re.compile(r"^([1-9][0-9]{0,9})\s*/\s*([A-Za-z]+)$")
_RATE_PERIODS = {
    "sec": 1,
    "second": 1,
    "min": 60,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
}
_MAX_RATE_LIMIT = 2_147_483_647


class RateConfigurationError(ValueError):
    """A rate setting cannot be enforced safely."""


def parse_rate(rate: object, *, setting_name: str = "rate limit") -> tuple[int, int]:
    """Parse ``<positive count>/<known period>`` without permissive fallbacks.

    Zero, negative, fractional, missing, oversized, and unknown-period values are invalid.
    Silently accepting one of those values could either remove an abuse control or make every
    request unavailable, so callers must fail startup or return a controlled temporary error.
    """

    if not isinstance(rate, str):
        raise RateConfigurationError(f"{setting_name} must use '<positive integer>/<sec|min|hour|day>'.")
    match = _RATE_PATTERN.fullmatch(rate.strip())
    if match is None:
        raise RateConfigurationError(f"{setting_name} must use '<positive integer>/<sec|min|hour|day>'.")

    limit = int(match.group(1))
    if limit > _MAX_RATE_LIMIT:
        raise RateConfigurationError(f"{setting_name} exceeds the supported request limit.")
    period = match.group(2).lower().rstrip("s")
    try:
        window = _RATE_PERIODS[period]
    except KeyError as exc:
        raise RateConfigurationError(f"{setting_name} must use a sec, minute, hour, or day period.") from exc
    return limit, window
