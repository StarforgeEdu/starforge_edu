"""Rate limiting for the layered (plain-Django) view style — replaces DRF throttles.

A fixed-window counter in the cache, keyed by ``scope:key`` (e.g. login attempts
per IP, or per username). Used two ways:

    @ratelimit(limit=10, window=60, scope="login_ip")   # by client IP (default key)
    def login_view(request): ...

    check_rate(scope="login_user", key=username, limit=5, window=60)  # in-view, by field

Over-limit raises ``ThrottledException`` (429 + Retry-After), rendered as JSON by
``core.middleware``. The counter is atomic (cache.add + incr), so concurrent bursts
can't slip past the cap.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _

from core.exceptions import ThrottledException
from core.utils import client_ip


def _consume(scope: str, key: str, limit: int, window: int) -> None:
    bucket = f"rl:{scope}:{key}"
    # cache.add sets the counter to 1 with the window TTL only if absent — the first
    # request in a window. Subsequent ones incr the existing counter (TTL preserved).
    if cache.add(bucket, 1, timeout=window):
        return
    try:
        count = cache.incr(bucket)
    except ValueError:  # key expired between add and incr — treat as a fresh window
        cache.set(bucket, 1, timeout=window)
        return
    if count > limit:
        raise ThrottledException(_("Too many requests. Please slow down."), wait=window)


def check_rate(*, scope: str, key: str, limit: int, window: int) -> None:
    """Imperative form — call inside a view once a body field (e.g. username) is known."""
    _consume(scope, key or "unknown", limit, window)


def ratelimit(
    *, limit: int, window: int, scope: str = "", key: Callable[[HttpRequest], str] | None = None
) -> Callable[..., Any]:
    """Decorator form — defaults to the client IP as the key."""

    def decorator(view_func: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        bucket_scope = scope or getattr(view_func, "__name__", "view")

        @wraps(view_func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            ident = key(request) if key is not None else client_ip(request)
            _consume(bucket_scope, ident or "anon", limit, window)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
