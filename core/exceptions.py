"""Project-wide exceptions and the DRF exception handler."""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
)
from rest_framework.exceptions import (
    NotFound as DRFNotFound,
)
from rest_framework.exceptions import (
    PermissionDenied as DRFPermissionDenied,
)
from rest_framework.exceptions import (
    Throttled as DRFThrottled,
)
from rest_framework.exceptions import (
    ValidationError as DRFValidationError,
)
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

logger = logging.getLogger("starforge.exceptions")

# Messages may be plain ``str`` or a lazy translation proxy (``gettext_lazy``);
# DRF's JSON encoder renders both. DoD #11 requires user-facing strings to be
# translatable, so every service raises with ``_()``-wrapped detail.
StrOrPromise = str | Promise


class StarforgeError(Exception):
    """Base for all domain errors. Carries a stable code + detail."""

    code: str = "error"
    status_code: int = status.HTTP_400_BAD_REQUEST
    default_detail: StrOrPromise = _("Something went wrong.")

    def __init__(
        self,
        detail: StrOrPromise | None = None,
        *,
        code: str | None = None,
        fields: dict[str, Any] | None = None,
    ) -> None:
        self.detail: StrOrPromise = detail if detail is not None else self.default_detail
        self.code = code or self.code
        self.fields = fields  # optional per-field detail (e.g. conflicting lesson ids)
        super().__init__(self.detail)


class ValidationException(StarforgeError):
    code = "validation_error"
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = _("Invalid input.")


class PermissionException(StarforgeError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _("You don't have permission to do that.")


class NotFoundException(StarforgeError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = _("Resource not found.")


class ThrottledException(StarforgeError):
    """429 with an optional ``wait`` (seconds) surfaced as a Retry-After header,
    matching DRF's own Throttled behavior so clients can branch uniformly."""

    code = "throttled"
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = _("Too many requests.")

    def __init__(
        self,
        detail: StrOrPromise | None = None,
        *,
        code: str | None = None,
        wait: float | None = None,
    ) -> None:
        self.wait = wait
        super().__init__(detail, code=code)


class ConflictException(StarforgeError):
    """Duplicate / overlap (e.g. schedule conflict, idempotency replay mismatch)."""

    code = "conflict"
    status_code = status.HTTP_409_CONFLICT
    default_detail = _("Conflict with the current state.")


class UnprocessableEntity(StarforgeError):
    """422 — a well-formed request the server understands but cannot act on
    (e.g. marking attendance for a student who is not an active member of the
    lesson's cohort). Distinct from 400 `validation_error` (malformed input)."""

    code = "unprocessable_entity"
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = _("The request could not be processed.")


class AuthenticationException(StarforgeError):
    """401 with a stable code — used by TenantAwareJWTAuthentication (TD-1) for
    `tenant_mismatch` / `token_stale`, surfaced through the same envelope."""

    code = "authentication_failed"
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = _("Authentication failed.")


class TenantContextMissing(StarforgeError):
    """Raised when tenant-scoped code runs without an active schema."""

    code = "tenant_required"
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = _("No active tenant context.")


def drf_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """Wrap DRF's default handler so all errors share an envelope."""

    # Imported lazily: core.exceptions is reachable from DEFAULT_AUTHENTICATION_CLASSES
    # during DRF's own `rest_framework.views` import, so a module-level import here
    # would close a circular-import loop.
    from rest_framework.views import exception_handler as drf_default_handler

    if isinstance(exc, StarforgeError):
        headers: dict[str, str] = {}
        wait = getattr(exc, "wait", None)
        if wait is not None:
            headers["Retry-After"] = str(int(wait))
        body: dict[str, Any] = {"success": False, "code": exc.code, "message": str(exc.detail)}
        exc_fields = getattr(exc, "fields", None)
        if exc_fields:
            body["errors"] = exc_fields
        return Response(body, status=exc.status_code, headers=headers or None)

    if isinstance(exc, PermissionDenied):
        return Response(
            {"success": False, "code": "forbidden", "message": str(exc) or str(_("Forbidden."))},
            status=status.HTTP_403_FORBIDDEN,
        )

    if isinstance(exc, Http404):
        return Response(
            {"success": False, "code": "not_found", "message": str(_("Resource not found."))},
            status=status.HTTP_404_NOT_FOUND,
        )

    response = drf_default_handler(exc, context)
    if response is None:
        logger.exception("Unhandled exception in view", extra={"context": context})
        return None

    # Normalize DRF exceptions to the flat project envelope with a stable, branchable
    # `code`, byte-compatible with core.responses.error() and core.middleware. Headers
    # DRF set (e.g. Retry-After on Throttled) are preserved.
    code, fields = _classify(exc)
    body = {"success": False, "code": code, "message": _detail_text(exc)}
    if fields is not None:
        body["errors"] = fields
    response.data = body
    return response


def _classify(exc: Exception) -> tuple[str, dict[str, Any] | None]:
    if isinstance(exc, DRFValidationError):
        detail = exc.detail
        fields = {k: _as_list(v) for k, v in detail.items()} if isinstance(detail, dict) else None
        return "validation_error", fields
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed, InvalidToken, TokenError)):
        return "authentication_failed", None
    if isinstance(exc, DRFPermissionDenied):
        return "forbidden", None
    if isinstance(exc, DRFNotFound):
        return "not_found", None
    if isinstance(exc, DRFThrottled):
        return "throttled", None
    return "api_error", None


def _detail_text(exc: Exception) -> str:
    if isinstance(exc, DRFValidationError):
        return str(_("Invalid input."))
    detail = getattr(exc, "detail", None)
    if isinstance(detail, (list, tuple)) and detail:
        return str(detail[0])
    if isinstance(detail, dict):
        return str(_("Invalid input."))
    return str(detail) if detail is not None else str(exc)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]
