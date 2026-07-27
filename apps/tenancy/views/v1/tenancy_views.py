"""Platform control-center HTTP views (layered, off DRF) — PUBLIC schema.

Mounted on the public urlconf at /api/v1/platform/. Every /centers/ view is
platform-staff-only (``require_platform_admin`` = session auth + is_staff,
reproducing the old ``IsAdminUser``); ``resolve`` (TD-19) is the single public,
anon-throttled exception. The heavy lifecycle logic lives in the preserved
``apps.tenancy.services`` domain functions behind ``ICenterService``.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.tenancy.interfaces.services import ICenterService
from apps.tenancy.platform_auth import require_platform_admin
from apps.tenancy.presenters import center_to_dict, domain_to_dict
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import bool_field, int_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.ratelimit import check_rate
from core.responses import created, error, paginated, success
from core.utils import client_ip

# TD-19 resolve is anonymous + anon-throttled. A module constant (not a captured
# decorator arg) so a test can lower it to force the 429 path.
RESOLVE_RATE_LIMIT = 60  # requests per window
RESOLVE_RATE_WINDOW = 60  # seconds


def _service() -> ICenterService:
    return container.resolve(ICenterService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _str_required(raw: Any, name: str, *, max_length: int) -> str:
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    if "\x00" in raw:
        raise _reject(name, "Null characters are not allowed.")
    value = raw.strip()
    if not value:
        raise _reject(name, "This field may not be blank.")
    if len(value) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return value


def _opt_str(data: dict[str, Any], name: str, *, max_length: int) -> str | None:
    """A PATCH-optional CharField: absent -> None (skip); explicit null -> 400
    (the column is NOT NULL); else a NUL-guarded, length-checked string."""
    if name not in data:
        return None
    raw = data[name]
    if not isinstance(raw, str):
        raise _reject(name, "This field must be a string.")
    if "\x00" in raw:
        raise _reject(name, "Null characters are not allowed.")
    if len(raw) > max_length:
        raise _reject(name, f"Ensure this field has no more than {max_length} characters.")
    return raw


def _validate_email_value(value: str, name: str) -> str:
    # DRF EmailField trims before validating/storing (trim_whitespace=True); mirror
    # that so a padded "a@b.com " is accepted+stored trimmed, not a 400.
    value = value.strip()
    if value:
        try:
            validate_email(value)
        except DjangoValidationError:
            raise _reject(name, "Enter a valid email address.") from None
    return value


# --- centers ---------------------------------------------------------------


def _create_data(request: HttpRequest) -> dict[str, Any]:
    data = read_json(request)
    return {
        "name": _str_required(_require(data, "name"), "name", max_length=200),
        # provision_center._validate_slug does the real slug validation
        # (lowercase / starts-with-letter / reserved / taken) -> 400.
        "slug": _str_required(_require(data, "slug"), "slug", max_length=100),
        "primary_domain": _str_required(_require(data, "primary_domain"), "primary_domain", max_length=253),
        "contact_name": str_field(data, "contact_name", max_length=200),
        "contact_phone": str_field(data, "contact_phone", max_length=32),
        "contact_email": _validate_email_value(str_field(data, "contact_email", max_length=254), "contact_email"),
    }


def _update_changes(request: HttpRequest) -> dict[str, Any]:
    """PATCH contact metadata (name + contact_*). Present-keys-only; lifecycle
    (is_active / trial) is never touched here (dedicated audited actions do it)."""
    data = read_json(request)
    changes: dict[str, Any] = {}
    if "name" in data:
        changes["name"] = _str_required(data["name"], "name", max_length=200)
    contact_name = _opt_str(data, "contact_name", max_length=200)
    if contact_name is not None:
        changes["contact_name"] = contact_name
    contact_phone = _opt_str(data, "contact_phone", max_length=32)
    if contact_phone is not None:
        changes["contact_phone"] = contact_phone
    contact_email = _opt_str(data, "contact_email", max_length=254)
    if contact_email is not None:
        changes["contact_email"] = _validate_email_value(contact_email, "contact_email")
    return changes


@csrf_exempt
@require_platform_admin
def centers_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        qs = apply_filters(
            request,
            _service().query(),
            filter_fields=("is_active", "on_trial"),
            search_fields=("name", "slug", "schema_name", "contact_email"),
            ordering_fields=("name", "created_at"),
            default_ordering="name",
        )
        items, total, page, size = paginate(request, qs)
        return paginated([center_to_dict(c) for c in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        center = _service().provision(data=_create_data(request), actor=request.user)
        return created(center_to_dict(center))
    return _method_not_allowed()


def _get_center(pk: int):
    center = _service().get(pk)
    if center is None:
        raise NotFoundException(code="not_found")
    return center


@csrf_exempt
@require_platform_admin
def center_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        return success(center_to_dict(_get_center(pk)))
    if request.method == "PATCH":
        center = _get_center(pk)
        center = _service().update_contact(center=center, changes=_update_changes(request))
        return success(center_to_dict(center))
    return _method_not_allowed()


@csrf_exempt
@require_platform_admin
def center_suspend_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    center = _get_center(pk)
    reason = str_field(read_json(request), "reason", max_length=512)
    center = _service().suspend(center=center, actor=request.user, reason=reason)
    return success(center_to_dict(center))


@csrf_exempt
@require_platform_admin
def center_activate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    center = _service().activate(center=_get_center(pk), actor=request.user)
    return success(center_to_dict(center))


@csrf_exempt
@require_platform_admin
def center_extend_trial_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    center = _get_center(pk)
    days = int_field(read_json(request), "days", required=True)
    if days is None or not (1 <= days <= 365):
        raise _reject("days", "Ensure this value is between 1 and 365.")
    center = _service().extend_trial(center=center, days=days, actor=request.user)
    return success(center_to_dict(center))


def _parse_days(raw: str | None) -> int:
    if not raw:
        return 30
    try:
        days = int(raw)
    except (TypeError, ValueError):
        raise _reject("days", "`days` must be an integer.") from None
    if days < 1 or days > 365:
        raise _reject("days", "`days` must be between 1 and 365.")
    return days


@csrf_exempt
@require_platform_admin
def center_usage_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    center = _get_center(pk)
    payload = _service().usage(center=center, days=_parse_days(request.GET.get("days")))
    return success(payload)


@csrf_exempt
@require_platform_admin
def center_impersonate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    center = _get_center(pk)
    user_id = int_field(read_json(request), "user_id", required=True)
    if user_id is None or user_id < 1:
        raise _reject("user_id", "Ensure this value is greater than or equal to 1.")
    result = _service().impersonate(center=center, user_id=user_id, impersonator=request.user)
    return success(result)


@csrf_exempt
@require_platform_admin
def center_domains_view(request: HttpRequest, pk: int) -> HttpResponse:
    center = _get_center(pk)
    if request.method in ("GET", "HEAD"):
        return success([domain_to_dict(d) for d in _service().list_domains(center=center)])
    if request.method == "POST":
        data = read_json(request)
        domain_name = _str_required(_require(data, "domain"), "domain", max_length=253)
        is_primary = bool_field(data, "is_primary")
        row = _service().add_domain(center=center, domain_name=domain_name, is_primary=is_primary)
        return created(domain_to_dict(row))
    return _method_not_allowed()


@csrf_exempt
@require_platform_admin
def center_set_primary_domain_view(request: HttpRequest, pk: int, domain_id: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    center = _get_center(pk)
    row = _service().set_primary_domain(center=center, domain_id=domain_id)
    return success(domain_to_dict(row))


# --- TD-19 resolve (public, anon-throttled) --------------------------------


@csrf_exempt
def resolve_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_rate(
        scope="platform_resolve",
        key=client_ip(request),
        limit=RESOLVE_RATE_LIMIT,
        window=RESOLVE_RATE_WINDOW,
    )
    slug = (request.GET.get("slug") or "").strip()
    if not slug:
        raise _reject("slug", "Query param `slug` is required.")
    return success(_service().resolve(slug=slug))
