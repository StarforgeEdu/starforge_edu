"""Platform-staff auth for the layered control-center views (PUBLIC schema).

These views run on the public schema and must reproduce the old DRF
``permission_classes = [IsAdminUser]`` exactly — NOT the tenant role matrix
(``check_perm``), which has no meaning for a public-schema platform user.

``require_platform_admin`` mirrors ``core.api_auth.require_auth`` (it reuses the
same custom session authenticator, so a TENANT-minted session key 401s here
because its row is not in the public ``Session`` table) and then enforces
``is_staff`` — DRF ``IsAdminUser`` is ``bool(user and user.is_staff)``, and a
superuser carries ``is_staff=True``. A non-staff authenticated user -> 403.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponseBase

from core.session_auth import SessionAuthentication

_authenticator = SessionAuthentication()

ViewFunc = Callable[..., HttpResponseBase]


def require_platform_admin(view_func: ViewFunc) -> ViewFunc:
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        from core.exceptions import AuthenticationException, PermissionException

        result = _authenticator.authenticate(request)
        if result is None:
            raise AuthenticationException(
                "Authentication credentials were not provided.", code="authentication_failed"
            )
        request.user, request.auth = result  # type: ignore[attr-defined]
        if not getattr(request.user, "is_staff", False):
            raise PermissionException("You do not have permission to perform this action.", code="forbidden")
        return view_func(request, *args, **kwargs)

    return wrapper
