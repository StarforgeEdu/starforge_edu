"""Billing platform HTTP views (layered, off DRF) — PUBLIC schema, staff-only.

Mounted on the public urlconf: /api/v1/platform/billing/ (plans/subscriptions-
by-center/usage/ai-charges/checkout) + /api/v1/platform/subscriptions/ (the flat
control-center subscription surface). Every view is platform-staff-only via the
shared ``require_platform_admin`` gate (session auth on the public schema +
is_staff — a tenant session key 401s here). The subscription state machine +
platform checkout live in the preserved ``apps.billing.services`` domain fns.
"""

from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from apps.billing.interfaces.services import IBillingService
from apps.billing.models import Subscription
from apps.billing.presenters import (
    ai_usage_charge_to_dict,
    plan_to_dict,
    subscription_to_dict,
    usage_snapshot_to_dict,
)
from apps.tenancy.platform_auth import require_platform_admin
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import int_field, read_json
from core.listing import apply_filters, paginate
from core.responses import error, paginated, success

_SUB_STATUSES = frozenset({Subscription.Status.ACTIVE, Subscription.Status.SUSPENDED})
_CHECKOUT_PROVIDERS = frozenset({"click", "payme", "uzum"})


def _service() -> IBillingService:
    return container.resolve(IBillingService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require_center(raw: str | None) -> int:
    if not raw:
        raise ValidationException("Query param `center` is required.", code="validation_error")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationException("`center` must be an integer.", code="validation_error") from None


# --- plans (read-only) -----------------------------------------------------


@csrf_exempt
@require_platform_admin
def plans_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    qs = apply_filters(
        request,
        _service().plans(),
        filter_fields=("is_active",),
        search_fields=("code", "name"),
        ordering_fields=("price_uzs", "code"),
        default_ordering="price_uzs",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([plan_to_dict(p) for p in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_platform_admin
def plan_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    plan = _service().plan(pk)
    if plan is None:
        raise NotFoundException(code="not_found")
    return success(plan_to_dict(plan))


# --- subscription changes (shared body validation) -------------------------


def _subscription_changes(request: HttpRequest) -> dict[str, Any]:
    """PATCH body: change plan and/or set status (active|suspended). Non-empty."""
    data = read_json(request)
    plan_code: str | None = None
    status: str | None = None
    provided = False
    if "plan_code" in data:
        raw = data["plan_code"]
        if not isinstance(raw, str) or not raw.strip():
            raise _reject("plan_code", "This field must be a non-empty string.")
        if "\x00" in raw:
            raise _reject("plan_code", "Must not contain NUL bytes.")
        plan_code = raw.strip()  # change_subscription resolves it -> 400 unknown_plan
        provided = True
    if "status" in data:
        raw_status = data["status"]
        # isinstance guard BEFORE the frozenset membership test: a list/dict value
        # would raise TypeError (unhashable) -> 500 instead of a clean 400.
        if not isinstance(raw_status, str) or raw_status not in _SUB_STATUSES:
            raise _reject("status", "Must be one of: active, suspended.")
        status = raw_status
        provided = True
    if not provided:
        raise ValidationException("Provide plan_code and/or status.", code="validation_error")
    return {"plan_code": plan_code, "status": status}


# --- subscriptions by CENTER id (/billing/subscriptions/<center_id>/) -------


@csrf_exempt
@require_platform_admin
def subscription_by_center_view(request: HttpRequest, center_id: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        sub = _service().subscription_by_center(center_id)
        if sub is None:
            raise NotFoundException(code="not_found")
        return success(subscription_to_dict(sub))
    if request.method == "PATCH":
        # Validate the body FIRST (matches the old serializer-before-service order:
        # an invalid/empty body 400s even for a center with no subscription);
        # change_subscription raises NotFoundException (404) when the sub is missing.
        changes = _subscription_changes(request)
        sub = _service().change_subscription(
            center_id=center_id, plan_code=changes["plan_code"], status=changes["status"]
        )
        return success(subscription_to_dict(sub))
    return _method_not_allowed()


# --- flat subscriptions by SUBSCRIPTION id (/platform/subscriptions/) -------


@csrf_exempt
@require_platform_admin
def platform_subscriptions_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    qs = apply_filters(
        request,
        _service().subscriptions(),
        filter_fields=("status", "plan"),
        ordering_fields=("center_id", "current_period_end"),
        default_ordering="center_id",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([subscription_to_dict(s) for s in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_platform_admin
def platform_subscription_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        sub = _service().subscription_by_pk(pk)
        if sub is None:
            raise NotFoundException(code="not_found")
        return success(subscription_to_dict(sub))
    if request.method == "PATCH":
        sub = _service().subscription_by_pk(pk)
        if sub is None:
            raise NotFoundException(code="not_found")
        changes = _subscription_changes(request)
        updated = _service().change_platform_subscription(
            sub=sub, plan_code=changes["plan_code"], status=changes["status"], actor=request.user
        )
        return success(subscription_to_dict(updated))
    return _method_not_allowed()


# --- usage / ai-charges (read-only) ----------------------------------------


@csrf_exempt
@require_platform_admin
def usage_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    center_id = _require_center(request.GET.get("center"))
    qs = _service().usage(center_id=center_id)
    items, total, page, size = paginate(request, qs)
    return paginated([usage_snapshot_to_dict(u) for u in items], total=total, page=page, page_size=size)


@csrf_exempt
@require_platform_admin
def ai_charges_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    center_id = _require_center(request.GET.get("center"))
    qs = _service().ai_charges(center_id=center_id)
    items, total, page, size = paginate(request, qs)
    return paginated([ai_usage_charge_to_dict(c) for c in items], total=total, page=page, page_size=size)


# --- checkout (mock platform payment) --------------------------------------


@csrf_exempt
@require_platform_admin
def checkout_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    data = read_json(request)
    center = int_field(data, "center", required=True)
    if center is None:
        raise _reject("center", "This field is required.")
    provider = data.get("provider", "payme")
    # isinstance guard before the frozenset membership test (a list/dict provider
    # would raise an unhashable-type TypeError -> 500 instead of a clean 400).
    if not isinstance(provider, str) or provider not in _CHECKOUT_PROVIDERS:
        raise _reject("provider", "Must be one of: click, payme, uzum.")
    sub = _service().checkout(center_id=center, provider=provider)
    return success(subscription_to_dict(sub))
