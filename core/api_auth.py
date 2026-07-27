"""Auth/permission decorators for the layered (plain-Django) view style.

Function-based views in the new architecture authenticate with these decorators
instead of DRF's permission machinery. They REUSE the custom session authenticator
and the role-permission matrix, so a migrated endpoint enforces the exact same
security as the DRF stack (session validation + tenant binding + the matrix).

    @require_auth
    @require_perm("students:write")
    def create_student_view(request): ...
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.http import HttpRequest, HttpResponseBase

from core.session_auth import SessionAuthentication

_authenticator = SessionAuthentication()

# HttpResponseBase (not HttpResponse) so a layered view may return a
# StreamingHttpResponse / FileResponse (e.g. the audit CSV export) as well.
ViewFunc = Callable[..., HttpResponseBase]


def require_auth(view_func: ViewFunc) -> ViewFunc:
    """Authenticate the JWT and attach ``request.user``/``request.auth``.

    Raises ``AuthenticationException`` (-> JSON 401) when no/invalid credentials are
    presented; tenant-mismatch and stale-token are raised by the authenticator with
    their own codes. The domain error is rendered as JSON by core.middleware."""

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        from core.exceptions import AuthenticationException

        result = _authenticator.authenticate(request)
        if result is None:
            raise AuthenticationException(
                "Authentication credentials were not provided.", code="authentication_failed"
            )
        request.user, request.auth = result  # type: ignore[attr-defined]
        return view_func(request, *args, **kwargs)

    return wrapper


_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def check_perm(request: HttpRequest, *codes: str) -> None:
    """Imperative authz check — raises on failure, returns None on pass.

    Faithfully mirrors the DRF ``RolePermission`` + ``DenyWriteForReadOnlyToken``:
    superuser bypass, A-2 per-center permission overrides, DIRECTOR (``*:*``) via
    ``has_permission_code``, and a 403 ``read_only_token`` for any write under a
    read-only impersonation session. Use directly when the required perm depends on the
    method (a collection view: read for GET, write for POST); ``require_perm`` wraps it."""
    from core.exceptions import PermissionException
    from core.permissions import (
        _request_overrides,
        get_user_roles,
        has_permission_code,
        is_read_only_token,
    )

    # The permission helpers are duck-typed on .user; a plain HttpRequest satisfies
    # them at runtime (typed as Request upstream).
    req: Any = request
    if request.method not in _SAFE_METHODS and is_read_only_token(req):
        raise PermissionException(code="read_only_token")
    if getattr(req.user, "is_superuser", False):
        return
    roles = get_user_roles(req)
    overrides = _request_overrides(req)
    if not any(has_permission_code(roles, code, overrides) for code in codes):
        raise PermissionException(
            "You do not have permission to perform this action.", code="forbidden"
        )


def deny_read_only_token(request: HttpRequest) -> None:
    """403 ``read_only_token`` if the caller holds a read-only impersonation session.

    For authenticated WRITE views that have no permission code to run ``check_perm``
    against (logout, password change) — the DRF stack blanket-denied writes under a
    read-only token via ``DenyWriteForReadOnlyToken``; a plain view that only
    ``@require_auth``s must reinstate that deny explicitly or an impersonating admin
    could force-logout or change a password from a read-only session."""
    from core.exceptions import PermissionException
    from core.permissions import is_read_only_token

    req: Any = request
    if is_read_only_token(req):
        raise PermissionException(code="read_only_token")


def require_perm(*codes: str) -> Callable[[ViewFunc], ViewFunc]:
    """Decorator form of ``check_perm`` for a single-perm view. Wraps a ``@require_auth``
    view (it reads ``request.user`` / resolved roles)."""

    def decorator(view_func: ViewFunc) -> ViewFunc:
        @wraps(view_func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            check_perm(request, *codes)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator
