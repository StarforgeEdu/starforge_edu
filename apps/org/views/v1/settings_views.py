"""CenterSettings endpoint — GET/PATCH the TD-13 singleton. org:read to read,
org:write to update."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.interfaces.services import ICenterSettingsService
from apps.org.presenters import settings_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import ValidationException
from core.http import read_json
from core.responses import error, success

_RESOURCE = "org"


def _service() -> ICenterSettingsService:
    return container.resolve(ICenterSettingsService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def settings_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, f"{_RESOURCE}:read")
        return success(settings_to_dict(_service().read()))
    if request.method in ("PATCH", "PUT"):
        check_perm(request, f"{_RESOURCE}:write")
        return success(settings_to_dict(_service().update(read_json(request))))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def system_availability_view(request: HttpRequest) -> HttpResponse:
    """Fault-isolation control (core.availability). GET the status of every app (up /
    degraded / disabled / unavailable, with warnings); PATCH ``{"disabled": [app, ...]}`` to
    turn apps off for THIS center at runtime (a disabled app 503s without falling the rest).
    org:read to view, org:write to change."""
    from core.availability import PROTECTED_APPS, set_tenant_disabled_apps, system_status

    if request.method in ("GET", "HEAD"):
        check_perm(request, f"{_RESOURCE}:read")
        return success({"apps": system_status()})
    if request.method in ("PATCH", "PUT"):
        check_perm(request, f"{_RESOURCE}:write")
        raw = read_json(request).get("disabled", [])
        if not isinstance(raw, list) or any(not isinstance(a, str) for a in raw):
            raise ValidationException(
                "disabled must be a list of app labels.",
                code="validation_error",
                fields={"disabled": ["Must be a list of app-label strings."]},
            )
        # Reject foundational apps with a clear error rather than silently stripping them:
        # disabling `org` would 503 THIS endpoint (it lives under /api/v1/org/) — an
        # unrecoverable self-lockout of the control plane.
        protected = sorted(set(raw) & PROTECTED_APPS)
        if protected:
            raise ValidationException(
                f"These apps are foundational and cannot be disabled: {', '.join(protected)}.",
                code="validation_error",
                fields={"disabled": [f"Cannot disable protected app(s): {', '.join(protected)}."]},
            )
        effective = set_tenant_disabled_apps(set(raw))
        return success({"disabled": sorted(effective), "apps": system_status()})
    return error("Method not allowed.", code="method_not_allowed", status=405)
