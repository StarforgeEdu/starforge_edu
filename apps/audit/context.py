"""Request-local actor metadata consumed by model audit receivers."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

_actor: ContextVar[Any] = ContextVar("audit_actor", default=None)
_request: ContextVar[Any] = ContextVar("audit_request", default=None)


def bind_request(request: Any) -> tuple[Token, Token]:
    """Bind a request and its current cookie-authenticated actor."""
    actor = getattr(request, "user", None)
    return _actor.set(actor), _request.set(request)


def bind_actor(actor: Any) -> None:
    """Refresh the actor after bearer authentication runs inside a view."""
    _actor.set(actor)


def reset_request(tokens: tuple[Token, Token]) -> None:
    actor_token, request_token = tokens
    _request.reset(request_token)
    _actor.reset(actor_token)


def current_actor() -> Any:
    return _actor.get()


def current_request() -> Any:
    return _request.get()
