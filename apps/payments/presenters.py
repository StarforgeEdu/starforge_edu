"""Payments response presenters (the DRF serializer output shapes).

Credential fields on ProviderConfig are WRITE-ONLY — never echoed back (TD-11).
"""

from __future__ import annotations

from decimal import Decimal

from apps.payments.models import FiscalReceipt, Payment, PaymentAttempt, ProviderConfig


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _money(value) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(Decimal("0.01")))


def provider_config_to_dict(cfg: ProviderConfig) -> dict:
    # Non-secret fields only — the *_secret_key / *_key / *_api_key credentials are
    # write-only and never serialized.
    return {
        "id": cfg.id,
        "provider": cfg.provider,
        "is_active": cfg.is_active,
        "click_service_id": cfg.click_service_id,
        "click_merchant_id": cfg.click_merchant_id,
        "payme_merchant_id": cfg.payme_merchant_id,
        "uzum_merchant_id": cfg.uzum_merchant_id,
        "created_at": _iso(cfg.created_at),
        "updated_at": _iso(cfg.updated_at),
    }


def fiscal_receipt_to_dict(receipt: FiscalReceipt) -> dict:
    return {
        "id": receipt.id,
        "status": receipt.status,
        "fiscal_sign": receipt.fiscal_sign,
        "qr_url": receipt.qr_url,
        "attempts": receipt.attempts,
        "submitted_at": _iso(receipt.submitted_at),
        "confirmed_at": _iso(receipt.confirmed_at),
    }


def payment_attempt_to_dict(attempt: PaymentAttempt) -> dict:
    return {
        "id": attempt.id,
        "attempt_no": attempt.attempt_no,
        "error_code": attempt.error_code,
        "created_at": _iso(attempt.created_at),
    }


def payment_read_to_dict(payment: Payment) -> dict:
    receipt = getattr(payment, "fiscal_receipt", None)
    return {
        "id": payment.id,
        "provider": payment.provider,
        "amount_uzs": _money(payment.amount_uzs),
        "currency": payment.currency,
        "status": payment.status,
        "provider_txn_id": payment.provider_txn_id,
        "provider_state": payment.provider_state,
        "account_ref": payment.account_ref,
        "allocation_status": payment.allocation_status,
        "cashier_shift": payment.cashier_shift_id,
        "payer": payment.payer_id,
        "paid_at": _iso(payment.paid_at),
        "fiscal_receipt": fiscal_receipt_to_dict(receipt) if receipt is not None else None,
        "attempts": [payment_attempt_to_dict(a) for a in payment.attempts.all()],
        "created_at": _iso(payment.created_at),
        "updated_at": _iso(payment.updated_at),
    }


def payment_list_to_dict(payment: Payment) -> dict:
    return {
        "id": payment.id,
        "provider": payment.provider,
        "amount_uzs": _money(payment.amount_uzs),
        "status": payment.status,
        "provider_txn_id": payment.provider_txn_id,
        "account_ref": payment.account_ref,
        "allocation_status": payment.allocation_status,
        "paid_at": _iso(payment.paid_at),
        "created_at": _iso(payment.created_at),
    }
