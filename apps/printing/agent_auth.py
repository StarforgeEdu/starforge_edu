"""Branch-agent auth decorator for the layered (plain-Django) agent endpoints.

Mirrors core.api_auth.require_auth but authenticates the ``Authorization: Agent <token>``
header via BranchAgentAuthentication (a BranchAgent, NOT a users.User). On success
``request.auth`` is the agent and ``request.user`` stays anonymous. A missing / non-Agent
/ malformed / unknown / revoked token -> 401 ``agent_token_invalid`` (rendered as JSON by
core.middleware).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponseBase

AgentViewFunc = Callable[..., HttpResponseBase]


def require_branch_agent(view_func: AgentViewFunc) -> AgentViewFunc:
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        from django.conf import settings

        from apps.printing.authentication import BranchAgentAuthentication
        from apps.printing.models import BranchAgent
        from core.exceptions import (
            AuthenticationException,
            ServiceUnavailableException,
        )
        from core.rate_config import RateConfigurationError, parse_rate
        from core.ratelimit import check_rate
        from core.utils import current_schema

        # authenticate() returns None for a missing / non-Agent header and RAISES
        # AuthenticationException(agent_token_invalid) for a malformed/unknown/revoked one.
        result = BranchAgentAuthentication().authenticate(request)
        if result is None or not isinstance(result[1], BranchAgent):
            raise AuthenticationException("Invalid agent token.", code="agent_token_invalid")
        request.user, request.auth = result  # type: ignore[attr-defined]
        # Agent-shaped traffic skipped the anonymous bucket before tenant
        # resolution, but every valid token now pays an exact tenant+device
        # allowance. Invalid or rotating tokens never reach this point and remain
        # bounded by the pre-authentication IP bucket.
        try:
            limit, window = parse_rate(
                getattr(settings, "API_RATELIMIT_AGENT", "600/min"),
                setting_name="API_RATELIMIT_AGENT",
            )
        except RateConfigurationError as exc:
            raise ServiceUnavailableException(
                "This operation is temporarily unavailable.",
                code="temporarily_unavailable",
            ) from exc
        check_rate(
            scope="api_branch_agent",
            key=f"{current_schema()}:{request.auth.pk}",  # type: ignore[attr-defined]
            limit=limit,
            window=window,
        )
        return view_func(request, *args, **kwargs)

    return wrapper
