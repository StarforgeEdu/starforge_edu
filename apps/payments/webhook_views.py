"""Public-schema payment webhook intake.

The ONE sanctioned public->tenant hop. These are PLAIN @csrf_exempt function views
with NO @require_auth — the "authentication" is the PROVIDER SIGNATURE, not a
session (providers push to us on the apex/public host). They return each provider's
EXACT expected response shape, NOT the success()/error() envelope.

Flow (CODE-GUIDE §3 item 5):
    resolve Center by slug (404 if absent/inactive)
      -> schema_context(center.schema_name)
        -> load that tenant's ProviderConfig
          -> verify the signature BEFORE touching any row
            -> record WebhookEvent (replay dedupe)
              -> process

Payme speaks its RPC-style Merchant API (HTTP 200 after tenant resolution,
including protocol errors). Click uses its provider-native numeric response. The old Uzum callback is
retained only as a test/development compatibility shim; production deliberately
disables it because it is not the current documented Uzum Merchant API contract.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import parse_qsl

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import schema_context

from apps.payments import services
from apps.payments.models import Payment, Provider, ProviderConfig, WebhookEvent
from core.exceptions import ConflictException, ValidationException
from infrastructure.http_client import InvalidProviderResponse, strict_json_loads

logger = logging.getLogger(__name__)

# Unauthenticated/invalidly-signed callbacks are rejected without a database
# insert. Persisting attacker-chosen nonces, even behind an IP bucket, is a
# distributed storage-exhaustion primitive. Only authenticated events enter the
# replay ledger, whose long-term size is bounded by the retention task.
WEBHOOK_MAX_BODY_BYTES = 64 * 1024
WEBHOOK_MAX_JSON_NODES = 512
WEBHOOK_MAX_JSON_DEPTH = 12
WEBHOOK_MAX_FORM_FIELDS = 32


class _WebhookPayloadError(ValueError):
    pass


def _resolve_center(center_slug: str):
    """Resolve an active Center by slug on the public schema. Returns None -> 404."""
    from apps.tenancy.models import Center

    return Center.objects.filter(slug=center_slug, is_active=True).first()


def _error(code: str, detail: str, *, http_status: int) -> JsonResponse:
    return JsonResponse({"error": {"code": code, "detail": detail}}, status=http_status)


def _bounded_body(request: HttpRequest) -> bytes:
    raw_length = request.META.get("CONTENT_LENGTH", "")
    if raw_length:
        try:
            content_length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise _WebhookPayloadError("Invalid Content-Length.") from exc
        if content_length < 0 or content_length > WEBHOOK_MAX_BODY_BYTES:
            raise _WebhookPayloadError("Webhook body is too large.")
    # Read one byte past the application limit directly from the request stream.
    # ``request.body`` materializes up to Django's much larger global upload
    # limit first, so a chunked request without Content-Length could otherwise
    # allocate megabytes merely to be rejected as a 64 KiB webhook.
    try:
        raw = request.read(WEBHOOK_MAX_BODY_BYTES + 1)
    except OSError as exc:
        raise _WebhookPayloadError("Webhook body could not be read.") from exc
    if not raw or len(raw) > WEBHOOK_MAX_BODY_BYTES:
        raise _WebhookPayloadError("Webhook body is empty or too large.")
    return raw


def _assert_payload_complexity(payload: dict[str, Any]) -> None:
    stack: list[tuple[Any, int]] = [(payload, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > WEBHOOK_MAX_JSON_NODES or depth > WEBHOOK_MAX_JSON_DEPTH:
            raise _WebhookPayloadError("Webhook JSON is too complex.")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or not key or len(key) > 128:
                    raise _WebhookPayloadError("Webhook JSON key is invalid.")
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise _WebhookPayloadError("Webhook JSON value is invalid.")


def _json_body(request: HttpRequest) -> tuple[dict[str, Any], bytes]:
    content_type = (request.content_type or "").lower()
    if content_type not in {"application/json", "application/json-rpc", "text/json"}:
        raise _WebhookPayloadError("Webhook Content-Type must be JSON.")
    raw = _bounded_body(request)
    try:
        data = strict_json_loads(raw)
    except InvalidProviderResponse as exc:
        raise _WebhookPayloadError("Webhook body is not strict JSON.") from exc
    if not isinstance(data, dict):
        raise _WebhookPayloadError("Webhook JSON must be an object.")
    _assert_payload_complexity(data)
    return data, raw


def _click_body(request: HttpRequest) -> tuple[dict[str, Any], bytes]:
    content_type = (request.content_type or "").lower()
    if content_type in {"application/json", "text/json"}:
        return _json_body(request)
    if content_type != "application/x-www-form-urlencoded":
        raise _WebhookPayloadError("Click webhook Content-Type is unsupported.")
    raw = _bounded_body(request)
    try:
        pairs = parse_qsl(
            raw.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=WEBHOOK_MAX_FORM_FIELDS,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _WebhookPayloadError("Click form body is invalid.") from exc
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload or not key or len(key) > 128 or len(value) > 512:
            raise _WebhookPayloadError("Click form fields are invalid.")
        payload[key] = value
    if not payload:
        raise _WebhookPayloadError("Click form body is empty.")
    return payload, raw


def _event_component(value: Any, *, max_length: int = 96) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _WebhookPayloadError("Provider event identifier is invalid.")
    text = str(value)
    if (
        not text
        or len(text) > max_length
        or text.strip() != text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise _WebhookPayloadError("Provider event identifier is invalid.")
    return text


def _invalid_event_id(provider: str, raw: bytes) -> str:
    digest = hashlib.sha256(provider.encode() + b"\0" + raw).hexdigest()
    return f"invalid:{digest[:48]}"


def _payme_error(code: int, message: str, *, rpc_id: Any = None) -> JsonResponse:
    localized = {"ru": message, "uz": message, "en": message}
    return JsonResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": localized}})


def _log_reference(provider: str, event_id: str) -> str:
    """Return a correlation value that does not disclose a provider identifier."""

    return hashlib.sha256(f"{provider}\0{event_id}".encode()).hexdigest()[:16]


def _click_response(
    payload: dict[str, Any],
    *,
    error: int,
    note: str,
    prepare_id: int | None = None,
    confirm_id: int | None = None,
) -> JsonResponse:
    body: dict[str, Any] = {
        "click_trans_id": payload.get("click_trans_id"),
        "merchant_trans_id": payload.get("merchant_trans_id"),
        "error": error,
        "error_note": note,
    }
    if prepare_id is not None:
        body["merchant_prepare_id"] = prepare_id
    if confirm_id is not None:
        body["merchant_confirm_id"] = confirm_id
    return JsonResponse(body)


def _config(provider: str) -> ProviderConfig | None:
    return ProviderConfig.objects.filter(provider=provider, is_active=True).first()


@csrf_exempt
def click_webhook_view(request: HttpRequest, center_slug: str) -> HttpResponse:
    if request.method != "POST":
        return _error("method_not_allowed", "Only POST is allowed.", http_status=405)
    center = _resolve_center(center_slug)
    if center is None:
        return _error("not_found", "Center not found.", http_status=404)
    try:
        payload, raw = _click_body(request)
    except _WebhookPayloadError:
        return JsonResponse({"error": -8, "error_note": "ERROR IN REQUEST"})

    from infrastructure.payments.click import (
        ACTION_COMPLETE,
        ACTION_PREPARE,
        ERROR_ALREADY_PAID,
        ERROR_FAILED_TO_UPDATE_USER,
        ERROR_IN_REQUEST,
        ERROR_INVALID_AMOUNT,
        ERROR_SIGN_CHECK_FAILED,
        ERROR_SUCCESS,
        ERROR_TRANSACTION_CANCELLED,
        ERROR_TRANSACTION_NOT_FOUND,
        ERROR_USER_NOT_FOUND,
        get_click_client,
    )

    with schema_context(center.schema_name):
        config = _config(Provider.CLICK)
        secret = getattr(config, "click_secret_key", "") if config else ""
        service_id = getattr(config, "click_service_id", "") if config else ""
        valid = bool(config) and get_click_client().verify_signature(
            payload=payload,
            secret_key=secret,
            expected_service_id=str(service_id),
        )
        if not valid:
            return _click_response(
                payload,
                error=ERROR_SIGN_CHECK_FAILED,
                note="SIGN CHECK FAILED",
            )

        try:
            event_id = (
                f"{_event_component(payload.get('click_trans_id'), max_length=64)}:"
                f"{_event_component(payload.get('action'), max_length=1)}"
            )
        except _WebhookPayloadError:
            event_id = _invalid_event_id(Provider.CLICK, raw)
            valid = False

        try:
            event, is_new = services.record_webhook_event(
                provider=Provider.CLICK,
                event_id=event_id,
                payload=payload,
                signature_valid=valid,
            )
        except (ConflictException, ValidationException):
            return _click_response(
                payload,
                error=ERROR_IN_REQUEST,
                note="CONFLICTING TRANSACTION",
            )
        except Exception:
            logger.exception(
                "Click webhook intake failed; event_ref=%s",
                _log_reference(Provider.CLICK, event_id),
            )
            return _click_response(
                payload,
                error=ERROR_FAILED_TO_UPDATE_USER,
                note="Processing error",
            )
        if not is_new:
            if event.status == WebhookEvent.Status.RECEIVED:
                # A concurrent worker owns this callback. A success acknowledgement
                # here could make Click stop retrying before any payment was committed.
                return _click_response(
                    payload,
                    error=ERROR_FAILED_TO_UPDATE_USER,
                    note="PROCESSING IN PROGRESS",
                )
            action = int(payload["action"])
            from apps.finance.models import Invoice

            invoice = Invoice.objects.filter(number=payload.get("merchant_trans_id", "")).first()
            if invoice is None:
                return _click_response(payload, error=ERROR_USER_NOT_FOUND, note="Unknown order")
            if action == ACTION_PREPARE:
                return _click_response(
                    payload,
                    error=ERROR_SUCCESS,
                    note="Already processed",
                    prepare_id=invoice.pk,
                )
            payment = Payment.objects.filter(
                provider=Provider.CLICK,
                provider_txn_id=str(payload.get("click_trans_id")),
                account_ref=invoice.number,
            ).first()
            if payment is None:
                return _click_response(
                    payload,
                    error=ERROR_TRANSACTION_NOT_FOUND,
                    note="Transaction does not exist",
                )
            return _click_response(
                payload,
                error=ERROR_SUCCESS,
                note="Already processed",
                confirm_id=payment.pk,
            )

        from apps.finance.models import Invoice

        invoice = Invoice.objects.filter(number=payload.get("merchant_trans_id", "")).first()
        if invoice is None:
            services.mark_webhook_rejected(event)
            return _click_response(
                payload,
                error=ERROR_USER_NOT_FOUND,
                note="Unknown order",
            )

        from apps.finance.selectors import OPEN_STATUSES

        if invoice.status not in OPEN_STATUSES:
            services.mark_webhook_rejected(event)
            code = ERROR_ALREADY_PAID if invoice.status == Invoice.Status.PAID else ERROR_USER_NOT_FOUND
            return _click_response(payload, error=code, note="Order is not payable")

        action = int(payload["action"])
        if action == ACTION_PREPARE:
            try:
                services.validate_provider_callback_amount(payload=payload, invoice=invoice)
            except ValidationException:
                services.mark_webhook_rejected(event)
                return _click_response(
                    payload,
                    error=ERROR_INVALID_AMOUNT,
                    note="Amount mismatch",
                )
            services.mark_webhook_processed(event)
            return _click_response(
                payload,
                error=ERROR_SUCCESS,
                note="Success",
                prepare_id=invoice.pk,
            )

        if action != ACTION_COMPLETE:  # defensive; signature validation restricts it
            services.mark_webhook_rejected(event)
            return _click_response(
                payload,
                error=ERROR_SIGN_CHECK_FAILED,
                note="Invalid action",
            )
        if str(payload.get("merchant_prepare_id")) != str(invoice.pk):
            services.mark_webhook_rejected(event)
            return _click_response(
                payload,
                error=ERROR_TRANSACTION_NOT_FOUND,
                note="Invalid prepare transaction",
            )
        raw_provider_error = payload.get("error")
        try:
            if raw_provider_error is None or isinstance(raw_provider_error, bool):
                raise ValueError
            provider_error = int(raw_provider_error)
            if str(raw_provider_error) != str(provider_error) or not -(2**31) <= provider_error <= 2**31 - 1:
                raise ValueError
        except (TypeError, ValueError):
            services.mark_webhook_rejected(event)
            return _click_response(payload, error=ERROR_IN_REQUEST, note="Invalid provider status")
        if provider_error != 0:
            # Click signs the transaction identifiers/amount/action/time but
            # reports the provider-side failure separately. Never credit an
            # invoice for a cancelled/failed completion callback.
            services.mark_webhook_rejected(event)
            return _click_response(
                payload,
                error=ERROR_TRANSACTION_CANCELLED,
                note="Transaction cancelled",
            )
        try:
            # Completion and replay-ledger transition share one commit boundary.
            with transaction.atomic():
                payment = services.process_click_complete(payload=payload, invoice=invoice)
                services.mark_webhook_processed(event)
        except ValidationException as exc:
            services.mark_webhook_rejected(event)
            is_amount_error = exc.code in {
                "amount_missing",
                "amount_invalid",
                "amount_mismatch",
                "click_amount_precision_unsupported",
            }
            return _click_response(
                payload,
                error=(ERROR_INVALID_AMOUNT if is_amount_error else ERROR_IN_REQUEST),
                note=("Amount mismatch" if is_amount_error else "Invalid transaction"),
            )
        except Exception:
            logger.exception(
                "Click webhook processing failed; event_ref=%s",
                _log_reference(Provider.CLICK, event_id),
            )
            services.mark_webhook_rejected(event)
            return _click_response(
                payload,
                error=ERROR_FAILED_TO_UPDATE_USER,
                note="Processing error",
            )
        return _click_response(
            payload,
            error=ERROR_SUCCESS,
            note="Success",
            confirm_id=payment.pk,
        )


@csrf_exempt
def payme_webhook_view(request: HttpRequest, center_slug: str) -> HttpResponse:
    if request.method != "POST":
        return _error("method_not_allowed", "Only POST is allowed.", http_status=405)
    # Payme always returns HTTP 200 ONCE the tenant is resolved (errors live in the
    # JSON-RPC `error` member). An unknown/inactive center is a routing failure -> the
    # TD-6 404 envelope, BEFORE any tenant context is entered.
    center = _resolve_center(center_slug)
    if center is None:
        return _error("not_found", "Center not found.", http_status=404)
    from infrastructure.payments.payme import ERR_INTERNAL, ERR_PARSE, get_payme_client

    try:
        body, raw = _json_body(request)
    except _WebhookPayloadError:
        return _payme_error(ERR_PARSE, "Invalid JSON-RPC request.")

    with schema_context(center.schema_name):
        config = _config(Provider.PAYME)
        key = getattr(config, "payme_key", "") if config else ""
        auth_header = request.META.get("HTTP_AUTHORIZATION")
        store = services.PaymeDBStore()
        client = get_payme_client()

        method = body.get("method")
        raw_params = body.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        raw_rpc_id = body.get("id")
        rpc_id = raw_rpc_id if isinstance(raw_rpc_id, int) and not isinstance(raw_rpc_id, bool) else None
        signature_valid = client.verify_auth(auth_header=auth_header, key=key)
        event: WebhookEvent | None = None
        is_new = False
        event_id = ""
        if method == "CreateTransaction" and params.get("id") is not None:
            try:
                event_id = _event_component(params.get("id"), max_length=64)
            except _WebhookPayloadError:
                event_id = _invalid_event_id(Provider.PAYME, raw)
            if signature_valid:
                try:
                    event, is_new = services.record_webhook_event(
                        provider=Provider.PAYME,
                        event_id=event_id,
                        payload=body,
                        signature_valid=signature_valid,
                        idempotent_retry=True,
                    )
                except (ConflictException, ValidationException):
                    return _payme_error(
                        ERR_PARSE,
                        "Conflicting transaction request.",
                        rpc_id=rpc_id,
                    )
                except Exception:
                    logger.exception(
                        "Payme webhook intake failed; event_ref=%s",
                        _log_reference(Provider.PAYME, event_id),
                    )
                    return _payme_error(
                        ERR_INTERNAL,
                        "Could not process transaction.",
                        rpc_id=rpc_id,
                    )
        try:
            response = client.handle(body=body, auth_header=auth_header, key=key, store=store)
        except Exception:
            logger.exception(
                "Payme webhook processing failed; event_ref=%s",
                _log_reference(
                    Provider.PAYME,
                    event_id or _invalid_event_id(Provider.PAYME, raw),
                ),
            )
            if event is not None and is_new:
                services.mark_webhook_rejected(event)
            return _payme_error(ERR_INTERNAL, "Could not process transaction.", rpc_id=rpc_id)
        try:
            if event is not None and is_new:
                if "result" in response:
                    services.mark_webhook_processed(event)
                else:
                    services.mark_webhook_rejected(event)
        except Exception:
            logger.exception(
                "Payme webhook audit transition failed; event_ref=%s",
                _log_reference(
                    Provider.PAYME,
                    event_id or _invalid_event_id(Provider.PAYME, raw),
                ),
            )
            return _payme_error(ERR_INTERNAL, "Could not process transaction.", rpc_id=rpc_id)
        return JsonResponse(response)


@csrf_exempt
def uzum_webhook_view(request: HttpRequest, center_slug: str) -> HttpResponse:
    if request.method != "POST":
        return _error("method_not_allowed", "Only POST is allowed.", http_status=405)
    if not getattr(settings, "UZUM_LEGACY_INTEGRATION_ENABLED", False):
        return _error(
            "provider_contract_unavailable",
            "This legacy callback contract is disabled.",
            http_status=503,
        )
    center = _resolve_center(center_slug)
    if center is None:
        return _error("not_found", "Center not found.", http_status=404)
    try:
        payload, _raw = _json_body(request)
    except _WebhookPayloadError:
        return _error("invalid_payload", "The callback body is invalid.", http_status=400)
    with schema_context(center.schema_name):
        config = _config(Provider.UZUM)
        api_key = getattr(config, "uzum_api_key", "") if config else ""
        # Uzum sends the HMAC in the X-Signature header, not the body.
        signature = request.META.get("HTTP_X_SIGNATURE", "")
        from infrastructure.payments.uzum import get_uzum_client

        valid = bool(config) and get_uzum_client().verify_signature(
            payload=payload, signature=signature, api_key=api_key
        )
        if not valid:
            return _error("invalid_signature", "Signature verification failed.", http_status=400)
        try:
            event_id = _event_component(
                payload.get("event_id") or payload.get("transaction_id") or payload.get("order_id"),
                max_length=64,
            )
        except _WebhookPayloadError:
            return _error("invalid_payload", "The callback identifier is invalid.", http_status=400)
        try:
            event, is_new = services.record_webhook_event(
                provider=Provider.UZUM,
                event_id=event_id,
                payload=payload,
                signature_valid=valid,
            )
        except (ConflictException, ValidationException):
            return _error(
                "conflicting_event",
                "The callback conflicts with an earlier event.",
                http_status=409,
            )
        except Exception:
            logger.exception(
                "Legacy Uzum webhook intake failed; event_ref=%s",
                _log_reference(Provider.UZUM, event_id),
            )
            return _error("processing_error", "Could not process the callback.", http_status=500)
        if not is_new:
            if event.status == WebhookEvent.Status.RECEIVED:
                return _error(
                    "processing_in_progress",
                    "The callback is still being processed.",
                    http_status=409,
                )
            return JsonResponse({"status": "duplicate"})

        if payload.get("status") != "PAID":
            services.mark_webhook_rejected(event)
            return _error("unsupported_event", "The callback event is unsupported.", http_status=400)

        from apps.finance.models import Invoice

        order_ref = payload.get("order_id") or payload.get("order_number") or payload.get("account", "")
        invoice = Invoice.objects.filter(number=order_ref).first()
        if invoice is None:
            # Unresolvable order: do NOT mark processed + ok — that swallows the
            # provider's corrective retry and silently loses a captured payment.
            # Reject so the event stays retryable (surfaces as a REJECTED row).
            services.mark_webhook_rejected(event)
            return _error("unknown_order", "No invoice matches this order.", http_status=400)
        try:
            with transaction.atomic():
                services.process_uzum_payment(payload=payload, invoice=invoice)
                services.mark_webhook_processed(event)
        except ValidationException:
            # Amount mismatch: reject (do not credit the invoice) and mark the event
            # rejected so a retry is not swallowed as a duplicate.
            services.mark_webhook_rejected(event)
            return _error(
                "amount_mismatch",
                "Reported amount does not match the invoice total.",
                http_status=400,
            )
        except Exception:
            # A transient failure (DB deadlock/serialization, or any non-validation
            # error) must NOT leave the event committed as RECEIVED — on retry it would
            # be flipped to DUPLICATE and swallowed, losing a captured payment. Reject
            # so the retry reprocesses cleanly.
            logger.exception(
                "Legacy Uzum webhook processing failed; event_ref=%s",
                _log_reference(Provider.UZUM, event_id),
            )
            services.mark_webhook_rejected(event)
            return _error("processing_error", "Could not process the callback.", http_status=400)
        return JsonResponse({"status": "ok"})
