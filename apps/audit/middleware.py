"""Bind request metadata for append-only model audit receivers."""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponseBase

from apps.audit.context import bind_request, reset_request


class AuditActorMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        tokens = bind_request(request)
        try:
            return self.get_response(request)
        finally:
            reset_request(tokens)
