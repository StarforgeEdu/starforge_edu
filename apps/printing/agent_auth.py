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
        from apps.printing.authentication import BranchAgentAuthentication
        from apps.printing.models import BranchAgent
        from core.exceptions import AuthenticationException

        # authenticate() returns None for a missing / non-Agent header and RAISES
        # AuthenticationException(agent_token_invalid) for a malformed/unknown/revoked one.
        result = BranchAgentAuthentication().authenticate(request)
        if result is None or not isinstance(result[1], BranchAgent):
            raise AuthenticationException("Invalid agent token.", code="agent_token_invalid")
        request.user, request.auth = result  # type: ignore[attr-defined]
        return view_func(request, *args, **kwargs)

    return wrapper
