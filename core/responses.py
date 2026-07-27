"""Standard JSON response envelope for the layered (non-DRF) API.

Every endpoint answers in ONE shape so mobile/web clients can branch on it:

    success:  {"success": true,  "data": <payload>, "message"?: str}
    error:    {"success": false, "message": str, "code"?: str, "errors"?: any}

``code`` is a stable, machine-branchable error key (mirrors core.exceptions), so
clients switch on it instead of parsing prose. Views build these with the helpers
below; ``core.middleware`` turns any uncaught domain error into the same shape.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpResponse, JsonResponse


def no_content() -> HttpResponse:
    """A bodyless 204 (e.g. logout, delete)."""
    return HttpResponse(status=204)


def success(data: Any = None, *, message: str = "", status: int = 200) -> JsonResponse:
    body: dict[str, Any] = {"success": True}
    if message:
        body["message"] = message
    if data is not None:
        body["data"] = data
    return JsonResponse(body, status=status)


def created(data: Any = None, *, message: str = "") -> JsonResponse:
    return success(data, message=message, status=201)


def paginated(
    items: list[Any], *, total: int, page: int, page_size: int, status: int = 200
) -> JsonResponse:
    """A page of ``items`` plus the cursor metadata a client needs to page on."""
    pages = (total + page_size - 1) // page_size if page_size else 0
    return JsonResponse(
        {
            "success": True,
            "data": items,
            "pagination": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "pages": pages,
                "has_next": page < pages,
                "has_prev": page > 1,
            },
        },
        status=status,
    )


def error(
    message: str = "Error", *, code: str = "error", errors: Any = None, status: int = 400
) -> JsonResponse:
    body: dict[str, Any] = {"success": False, "code": code, "message": message}
    if errors is not None:
        body["errors"] = errors
    return JsonResponse(body, status=status)


def validation_error(errors: Any, *, message: str = "Validation failed") -> JsonResponse:
    return error(message, code="validation_error", errors=errors, status=422)


def unauthorized(message: str = "Authentication credentials were not provided or are invalid.") -> JsonResponse:
    return error(message, code="authentication_failed", status=401)


def forbidden(message: str = "You do not have permission to perform this action.") -> JsonResponse:
    return error(message, code="forbidden", status=403)


def not_found(message: str = "Resource not found.") -> JsonResponse:
    return error(message, code="not_found", status=404)
