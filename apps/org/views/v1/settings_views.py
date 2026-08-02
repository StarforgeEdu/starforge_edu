"""Organization-wide settings and runtime-isolation control endpoints."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.interfaces.services import ICenterSettingsService
from apps.org.presenters import settings_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import ValidationException
from core.http import read_json, reject_unknown_fields
from core.responses import error, success
from core.scoping import assert_permission_organization_scope

_SETTINGS_READ = "organization_settings:read"
_SETTINGS_WRITE = "organization_settings:write"
_SYSTEM_READ = "system:read"
_SYSTEM_WRITE = "system:write"


def _service() -> ICenterSettingsService:
    return container.resolve(ICenterSettingsService)  # type: ignore[type-abstract]


@csrf_exempt
@require_auth
def settings_view(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        check_perm(request, _SETTINGS_READ)
        assert_permission_organization_scope(request, permission=_SETTINGS_READ)
        return success(settings_to_dict(_service().read()))
    if request.method in ("PATCH", "PUT"):
        check_perm(request, _SETTINGS_WRITE)
        assert_permission_organization_scope(request, permission=_SETTINGS_WRITE)
        return success(settings_to_dict(_service().update(read_json(request))))
    return error("Method not allowed.", code="method_not_allowed", status=405)


@csrf_exempt
@require_auth
def system_availability_view(request: HttpRequest) -> HttpResponse:
    """Fault-isolation control (core.availability). GET the status of every app (up /
    degraded / disabled / unavailable, with warnings); PATCH ``{"disabled": [app, ...]}`` to
    turn apps off for THIS center at runtime (a disabled app 503s without falling the rest).
    This is tenant-global control-plane state and therefore requires an
    organization-wide grant; branch-scoped organization permissions are never
    sufficient."""
    from core.availability import APP_MOUNTS, PROTECTED_APPS, set_tenant_disabled_apps, system_status

    if request.method in ("GET", "HEAD"):
        check_perm(request, _SYSTEM_READ)
        assert_permission_organization_scope(request, permission=_SYSTEM_READ)
        return success({"apps": system_status()})
    if request.method in ("PATCH", "PUT"):
        check_perm(request, _SYSTEM_WRITE)
        assert_permission_organization_scope(request, permission=_SYSTEM_WRITE)
        body = read_json(request)
        reject_unknown_fields(body, allowed={"disabled"})
        raw = body.get("disabled", [])
        if not isinstance(raw, list) or any(not isinstance(a, str) for a in raw):
            raise ValidationException(
                "disabled must be a list of app labels.",
                code="validation_error",
                fields={"disabled": ["Must be a list of app-label strings."]},
            )
        normalized = [app.strip() for app in raw]
        if any(not app for app in normalized) or len(normalized) != len(set(normalized)):
            raise ValidationException(
                "disabled contains blank or duplicate app labels.",
                code="validation_error",
                fields={"disabled": ["Use unique, non-empty app labels."]},
            )
        unknown = sorted(set(normalized) - set(APP_MOUNTS.values()))
        if unknown:
            raise ValidationException(
                "disabled contains unknown app labels.",
                code="validation_error",
                fields={"disabled": [f"Unknown app labels: {', '.join(unknown)}."]},
            )
        # Reject foundational apps with a clear error rather than silently stripping them:
        # disabling `org` would 503 THIS endpoint (it lives under /api/v1/org/) — an
        # unrecoverable self-lockout of the control plane.
        protected = sorted(set(normalized) & PROTECTED_APPS)
        if protected:
            raise ValidationException(
                f"These apps are foundational and cannot be disabled: {', '.join(protected)}.",
                code="validation_error",
                fields={"disabled": [f"Cannot disable protected app(s): {', '.join(protected)}."]},
            )
        effective = set_tenant_disabled_apps(set(normalized))
        return success({"disabled": sorted(effective), "apps": system_status()})
    return error("Method not allowed.", code="method_not_allowed", status=405)
