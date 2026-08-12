"""Organization-wide settings and runtime-isolation control endpoints."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.org.interfaces.services import ICenterSettingsService
from apps.org.openapi_contracts import (
    STAFF_APP_STATUS_GET_CONTRACT,
    STAFF_APP_STATUS_HEAD_CONTRACT,
)
from apps.org.presenters import settings_to_dict
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import PermissionException, ValidationException
from core.http import read_json, reject_unknown_fields
from core.openapi_contracts import openapi_contract
from core.permissions import get_role_memberships
from core.responses import error, success
from core.role_principals import STAFF_PRINCIPAL_KINDS, request_role_principal
from core.scoping import assert_permission_organization_scope

_SETTINGS_READ = "organization_settings:read"
_SETTINGS_WRITE = "organization_settings:write"
_SYSTEM_READ = "system:read"
_SYSTEM_WRITE = "system:write"

_STAFF_APP_FEATURES = (
    ("ai", "ai"),
    ("notifications", "notifications"),
    ("groups", "cohorts"),
    ("attendance", "attendance"),
    ("library", "content"),
    ("printing", "printing"),
    ("messaging", "messaging"),
    ("tasks", "staff_tasks"),
)
_STAFF_APP_BLOCKED_ROLE_TOKENS = frozenset({"ceo", "owner", "director", "manager", "administrator", "admin"})
_STAFF_APP_BLOCKED_ROLE_PHRASES = frozenset({"chief-executive", "head-of-department", "head-of-dept"})


def _service() -> ICenterSettingsService:
    return container.resolve(ICenterSettingsService)  # type: ignore[type-abstract]


def _normalized_staff_role_label(value: object) -> str:
    """Match the staff mobile application's fail-closed role policy."""

    import re

    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()))


def _assert_staff_app_account(request: HttpRequest) -> None:
    """Accept only the exact role-native accounts allowed to sign into Staff.

    The endpoint deliberately does not use ``system:read``: that permission grants
    access to a control-plane status surface with operational detail. This check is
    identity-only and exposes a much smaller product-level projection.
    """

    request_role_principal(
        request,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
        error_code="staff_app_account_required",
    )
    labels: list[object] = []
    for membership in get_role_memberships(request):
        account_type = membership.account_type
        if account_type is None:
            labels.append(membership.role)
        else:
            labels.extend((account_type.slug, account_type.name))
    for label in labels:
        normalized = _normalized_staff_role_label(label)
        tokens = frozenset(token for token in normalized.split("-") if token)
        if tokens & _STAFF_APP_BLOCKED_ROLE_TOKENS or any(
            phrase in normalized for phrase in _STAFF_APP_BLOCKED_ROLE_PHRASES
        ):
            raise PermissionException(
                "This role account uses a different application.",
                code="staff_app_account_required",
            )


def _mobile_availability_status(status: str) -> str:
    from core.availability import STATUS_DEGRADED, STATUS_UP

    if status == STATUS_UP:
        return "available"
    if status == STATUS_DEGRADED:
        return "degraded"
    return "unavailable"


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


@openapi_contract(
    path="/api/v1/org/app-status/",
    operations=(STAFF_APP_STATUS_GET_CONTRACT, STAFF_APP_STATUS_HEAD_CONTRACT),
)
@csrf_exempt
@require_auth
def staff_app_status_view(request: HttpRequest) -> HttpResponse:
    """Read-only, privacy-minimized feature status for the Staff mobile app."""

    if request.method not in ("GET", "HEAD"):
        return error("Method not allowed.", code="method_not_allowed", status=405)
    _assert_staff_app_account(request)
    from core.availability import resolve_status

    features = [
        {
            "feature": feature,
            "status": _mobile_availability_status(resolve_status(app)[0]),
        }
        for feature, app in _STAFF_APP_FEATURES
    ]
    response = success({"features": features})
    response["Cache-Control"] = "no-store"
    return response


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
