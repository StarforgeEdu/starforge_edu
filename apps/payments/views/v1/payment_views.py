"""Payments staff (tenant-side) HTTP views (layered, off DRF).

Provider-credential CRUD (secrets write-only) + the payment log with the checkout /
cash / allocate / refund / reconciliation / receipt actions. The public provider
webhooks live in apps/payments/webhook_views.py (a separate public-schema surface).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from apps.payments.interfaces.services import IPaymentService, IProviderConfigService
from apps.payments.models import Provider
from apps.payments.presenters import (
    payment_list_to_dict,
    payment_read_to_dict,
    provider_config_to_dict,
)
from core.api_auth import check_perm, require_auth
from core.container import container
from core.exceptions import NotFoundException, ValidationException
from core.http import decimal_field, read_json, str_field
from core.listing import apply_filters, paginate
from core.responses import created, error, no_content, paginated, success
from core.utils import current_schema, stable_hash
from infrastructure.storage.s3_client import presign_download

_PROVIDERS = set(Provider.values)
_CHECKOUT_PROVIDERS = {"click", "payme", "uzum"}
# Idempotency window (seconds) for a headerless cash payment: a resubmit within this
# window coalesces; a genuine repeat outside it records separately. The client
# Idempotency-Key header is the precise dedupe; this is only the no-header fallback.
_CASH_IDEMPOTENCY_WINDOW_S = 60
# ProviderConfig string fields (write-only credentials + merchant ids), each with its
# model max_length. Accepted on create/update but never echoed by the read presenter.
_CONFIG_STR_FIELDS = {
    "click_service_id": 64,
    "click_merchant_id": 64,
    "payme_merchant_id": 64,
    "uzum_merchant_id": 64,
    "click_secret_key": 255,
    "payme_key": 255,
    "payme_test_key": 255,
    "uzum_api_key": 255,
}


def _config_service() -> IProviderConfigService:
    return container.resolve(IProviderConfigService)  # type: ignore[type-abstract]


def _payment_service() -> IPaymentService:
    return container.resolve(IPaymentService)  # type: ignore[type-abstract]


def _method_not_allowed() -> HttpResponse:
    return error("Method not allowed.", code="method_not_allowed", status=405)


def _reject(field: str, message: str) -> ValidationException:
    return ValidationException("Invalid input.", code="validation_error", fields={field: [message]})


def _require(data: dict[str, Any], name: str) -> Any:
    if name not in data or data[name] is None:
        raise _reject(name, "This field is required.")
    return data[name]


def _int(raw: Any, name: str, *, min_value: int | None = None) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise _reject(name, "A valid integer is required.")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise _reject(name, "A valid integer is required.") from None
    if min_value is not None and value < min_value:
        raise _reject(name, f"Ensure this value is greater than or equal to {min_value}.")
    return value


def _choice(raw: Any, name: str, choices) -> str:
    if not isinstance(raw, str) or raw not in choices:
        raise _reject(name, f"Must be one of: {', '.join(sorted(choices))}.")
    return raw


def _bool(raw: Any, name: str) -> bool:
    # Coerce DRF-BooleanField-compatible strings; an explicit null (or anything else)
    # is a 400 — never a silent coerce to the default (which would deactivate a config).
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ("true", "1", "yes", "y", "t", "on"):
            return True
        if value in ("false", "0", "no", "n", "f", "off"):
            return False
    raise _reject(name, "Must be a valid boolean.")


# --- provider configs ------------------------------------------------------


def _config_write_data(request: HttpRequest, *, partial: bool) -> dict[str, Any]:
    data = read_json(request)
    out: dict[str, Any] = {}
    if "provider" in data:
        out["provider"] = _choice(data["provider"], "provider", _PROVIDERS)
    elif not partial:
        raise _reject("provider", "This field is required.")
    if "is_active" in data:
        # Explicit null must not silently deactivate the config (NOT-NULL column).
        out["is_active"] = _bool(data["is_active"], "is_active")
    for field, max_length in _CONFIG_STR_FIELDS.items():
        if field not in data:
            continue
        raw = data[field]
        # Explicit null must not silently WIPE a stored credential (a blanked secret
        # would make every subsequent provider signature check fail). "" is allowed
        # (the columns are blank=True), matching the old serializer.
        if not isinstance(raw, str):
            raise _reject(field, "This field must be a string.")
        if len(raw) > max_length:
            raise _reject(field, f"Ensure this field has no more than {max_length} characters.")
        out[field] = raw
    return out


@csrf_exempt
@require_auth
def provider_configs_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method in ("GET", "HEAD"):
        check_perm(request, "payments:read")
        qs = apply_filters(
            request,
            _config_service().list_configs(),
            filter_fields=("provider", "is_active"),
            ordering_fields=("provider",),
        )
        items, total, page, size = paginate(request, qs)
        return paginated([provider_config_to_dict(c) for c in items], total=total, page=page, page_size=size)
    if request.method == "POST":
        check_perm(request, "payments:write")
        cfg = _config_service().create(data=_config_write_data(request, partial=False))
        return created(provider_config_to_dict(cfg))
    return _method_not_allowed()


@csrf_exempt
@require_auth
def provider_config_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    read = request.method in ("GET", "HEAD")
    check_perm(request, "payments:read" if read else "payments:write")
    cfg = _config_service().get(pk=pk)
    if cfg is None:
        raise NotFoundException(code="not_found")
    if read:
        return success(provider_config_to_dict(cfg))
    if request.method in ("PUT", "PATCH"):
        changes = _config_write_data(request, partial=(request.method == "PATCH"))
        return success(provider_config_to_dict(_config_service().update(cfg, changes=changes)))
    if request.method == "DELETE":
        _config_service().delete(cfg)
        return no_content()
    return _method_not_allowed()


# --- payments (log + actions) ----------------------------------------------


@csrf_exempt
@require_auth
def payments_collection_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "payments:read")
    qs = apply_filters(
        request,
        _payment_service().list_payments(),
        filter_fields=("provider", "status", "allocation_status"),
        ordering_fields=("created_at", "paid_at", "amount_uzs"),
        default_ordering="-created_at",
    )
    items, total, page, size = paginate(request, qs)
    return paginated([payment_list_to_dict(p) for p in items], total=total, page=page, page_size=size)


def _get_payment(pk: int):
    payment = _payment_service().get(pk=pk)
    if payment is None:
        raise NotFoundException(code="not_found")
    return payment


@csrf_exempt
@require_auth
def payment_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "payments:read")
    return success(payment_read_to_dict(_get_payment(pk)))


@csrf_exempt
@require_auth
def payment_checkout_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "payments:write")
    data = read_json(request)
    invoice = _int(_require(data, "invoice"), "invoice")
    provider = _choice(_require(data, "provider"), "provider", _CHECKOUT_PROVIDERS)
    # Idempotency-Key header (TASKS §16) or a derived stable key per (invoice, provider, user).
    idem = request.headers.get("Idempotency-Key") or stable_hash(
        f"checkout:{current_schema()}:{invoice}:{provider}:{request.user.pk}"
    )
    result = _payment_service().checkout(
        invoice_id=invoice, provider=provider, idempotency_key=idem, payer=request.user
    )
    return created(result)


@csrf_exempt
@require_auth
def payment_cash_view(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "payments:write")
    data = read_json(request)
    invoice = _int(_require(data, "invoice"), "invoice")
    amount_uzs = decimal_field(data, "amount_uzs", max_digits=18, decimal_places=2)
    # Cash idempotency (the correct contract is a client-supplied Idempotency-Key per POS
    # action — see the flagged follow-up). WITHOUT a header we fall back to an idempotency
    # TIME WINDOW: key on (invoice, cashier, amount, 60s bucket). This is the standard way
    # to distinguish an accidental resubmit from a genuine repeat WITHOUT a client key —
    # the two prior attempts each failed one side: a fully-unique key double-credited a
    # double-click; a bucketless amount key silently swallowed legitimate equal-amount
    # installments taken minutes/days apart. With a window, a double-click/retry (same
    # second) coalesces while installments (different windows) each record. Residuals
    # (double-click straddling the boundary; two equal payments within one 60s window) are
    # pathological and are what the client Idempotency-Key exists to eliminate precisely.
    bucket = int(timezone.now().timestamp()) // _CASH_IDEMPOTENCY_WINDOW_S
    idem = request.headers.get("Idempotency-Key") or stable_hash(
        f"cash:{current_schema()}:{invoice}:{request.user.pk}:{amount_uzs}:{bucket}"
    )
    payment = _payment_service().cash(
        invoice_id=invoice, cashier=request.user, amount_uzs=amount_uzs, idempotency_key=idem
    )
    return created(payment_read_to_dict(payment))


def _parse_allocations(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _require(data, "allocations")
    if not isinstance(raw, list) or not raw:
        raise _reject("allocations", "A non-empty list of allocations is required.")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _reject(f"allocations[{index}]", "Each allocation must be an object.")
        invoice = _int(_require(item, "invoice"), f"allocations[{index}].invoice")
        amount = decimal_field(item, "amount", max_digits=18, decimal_places=2)
        if amount is None:
            raise _reject(f"allocations[{index}].amount", "This field is required.")
        out.append({"invoice": invoice, "amount": amount})
    return out


@csrf_exempt
@require_auth
def payment_allocate_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "payments:write")
    payment = _get_payment(pk)
    allocations = _parse_allocations(read_json(request))
    result = _payment_service().allocate(payment_id=payment.pk, allocations=allocations)
    return success(payment_read_to_dict(result))


@csrf_exempt
@require_auth
def payment_refund_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method != "POST":
        return _method_not_allowed()
    check_perm(request, "payments:write")
    payment = _get_payment(pk)
    data = read_json(request)
    amount = decimal_field(data, "amount", max_digits=18, decimal_places=2)
    reason = str_field(data, "reason", max_length=255)
    result = _payment_service().refund(payment_id=payment.pk, amount_uzs=amount, reason=reason)
    return success(payment_read_to_dict(result))


@csrf_exempt
@require_auth
def payment_reconciliation_view(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "payments:read")
    raw = request.GET.get("date")
    if raw:
        try:
            on = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationException(
                "date must be YYYY-MM-DD.", code="validation_error", fields={"date": ["invalid"]}
            ) from exc
    else:
        from django.utils import timezone

        on = timezone.localdate()
    return success(_payment_service().reconciliation(on=on))


@csrf_exempt
@require_auth
def payment_receipt_view(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    check_perm(request, "payments:read")
    payment = _get_payment(pk)
    receipt = getattr(payment, "fiscal_receipt", None)
    if receipt is None:
        raise NotFoundException("No fiscal receipt for this payment yet.", code="not_found")
    key = (receipt.payload or {}).get("pdf_key")
    if key:
        return success({"url": presign_download(key, expires_in=600)})
    from apps.payments import services as domain

    domain.enqueue_receipt_pdf(payment.pk, current_schema())
    return success({"status": "generating"}, status=202)
