"""Payments write-side services (D3-B-6..11).

All writes here: idempotency-keyed Payment creation, the Payme JSON-RPC store,
webhook intake with replay protection, checkout + auto-allocation (Lane A's
``allocate_payment`` via LAZY import — it lands in a different lane), the refund
flow (drives ``finance.Refund`` via lazy import), and the single chokepoint that
flips a Payment to completed/failed and emits the matching signal exactly once.

Cross-app SERVICE calls are imported LAZILY inside the function (Lane A merges
before B but is built in parallel) — never at module top.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.payments.models import (
    FiscalReceipt,
    Payment,
    PaymentAttempt,
    Provider,
    ProviderConfig,
    WebhookEvent,
)
from apps.payments.signals import payment_completed, payment_failed
from core.exceptions import NotFoundException, UnprocessableEntity, ValidationException
from core.utils import current_schema, stable_hash
from infrastructure.payments.payme import (
    ERR_ACCOUNT_NOT_FOUND,
    STATE_CANCELLED,
    STATE_CANCELLED_AFTER_PERFORM,
    STATE_CREATED,
    STATE_PERFORMED,
    PaymeError,
)

_TIYIN = Decimal("100")


# ---------------------------------------------------------------------------
# Idempotent payment creation (D3-B-6)
# ---------------------------------------------------------------------------
@transaction.atomic
def get_or_create_payment(
    *,
    idempotency_key: str,
    provider: str,
    amount_uzs: Decimal,
    account_ref: str = "",
    payer=None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Payment, bool]:
    """Return ``(payment, created)``. The same idempotency key always returns the
    existing row — never a duplicate (the unique constraint is the backstop).
    """
    if not idempotency_key:
        raise ValidationException(_("idempotency_key is required."), fields={"idempotency_key": ["required"]})
    existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False
    payment = Payment.objects.create(
        idempotency_key=idempotency_key,
        provider=provider,
        amount_uzs=amount_uzs,
        account_ref=account_ref,
        payer=payer,
        metadata=metadata or {},
    )
    return payment, True


def _record_attempt(
    payment: Payment, *, request_payload: dict, response_payload: dict, error_code: str = ""
) -> None:
    attempt_no = payment.attempts.count() + 1
    PaymentAttempt.objects.create(
        payment=payment,
        attempt_no=attempt_no,
        request_payload=request_payload,
        response_payload=response_payload,
        error_code=error_code,
    )


# ---------------------------------------------------------------------------
# State transition chokepoint + signals (D3-B-11)
# ---------------------------------------------------------------------------
def _invoice_and_student_for(payment: Payment) -> tuple[int | None, int | None]:
    """Best-effort resolution of (invoice_id, student_id) for signal kwargs.
    Lazy finance import — finance lands in another lane."""
    invoice_id = payment.metadata.get("invoice_id")
    student_id = payment.metadata.get("student_id")
    if invoice_id and not student_id:
        try:
            from apps.finance.models import Invoice

            inv = Invoice.objects.filter(pk=invoice_id).values_list("student_id", flat=True).first()
            student_id = inv
        except Exception:  # finance not migrated yet / row gone — signal still fires
            student_id = None
    return invoice_id, student_id


@transaction.atomic
def mark_payment_completed(
    *, payment_id: int, provider_txn_id: str = "", auto_allocate: bool = True
) -> Payment:
    """Flip a Payment to completed ONCE. Idempotent: a re-call on an already
    completed payment is a no-op (no second signal, no second allocation)."""
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status == Payment.Status.COMPLETED:
        return payment
    payment.status = Payment.Status.COMPLETED
    payment.paid_at = timezone.now()
    if provider_txn_id:
        payment.provider_txn_id = provider_txn_id
    payment.save(update_fields=["status", "paid_at", "provider_txn_id", "updated_at"])

    if auto_allocate:
        _auto_allocate(payment)

    schema = current_schema()
    invoice_id, student_id = _invoice_and_student_for(payment)
    amount = str(payment.amount_uzs)
    transaction.on_commit(
        lambda: payment_completed.send(
            sender=Payment,
            payment_id=payment.pk,
            invoice_id=invoice_id,
            student_id=student_id,
            amount_uzs=amount,
            schema_name=schema,
        )
    )
    # Post-payment fiscalization (D3-B-9) — Celery, idempotent.
    transaction.on_commit(lambda: _enqueue_fiscalization(payment.pk, schema))
    return payment


@transaction.atomic
def mark_payment_failed(*, payment_id: int, cancel_reason: int | None = None) -> Payment:
    """Flip a Payment to failed ONCE (no second signal on re-call)."""
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status in (Payment.Status.FAILED, Payment.Status.CANCELLED):
        return payment
    payment.status = Payment.Status.FAILED
    if cancel_reason is not None:
        payment.cancel_reason = cancel_reason
    payment.save(update_fields=["status", "cancel_reason", "updated_at"])

    schema = current_schema()
    invoice_id, student_id = _invoice_and_student_for(payment)
    amount = str(payment.amount_uzs)
    transaction.on_commit(
        lambda: payment_failed.send(
            sender=Payment,
            payment_id=payment.pk,
            invoice_id=invoice_id,
            student_id=student_id,
            amount_uzs=amount,
            schema_name=schema,
        )
    )
    return payment


def _enqueue_fiscalization(payment_id: int, schema: str) -> None:
    from celery_tasks.payment_tasks import fiscalize_payment

    fiscalize_payment.delay(payment_id, _schema_name=schema)


# ---------------------------------------------------------------------------
# Auto-allocation (D3-B-7) — Lane A's allocate_payment via lazy import
# ---------------------------------------------------------------------------
def _auto_allocate(payment: Payment) -> None:
    """If the payment amount matches a single invoice exactly, auto-allocate via
    Lane A's service; otherwise flag for manual review."""
    invoice_id = payment.metadata.get("invoice_id")
    if not invoice_id:
        payment.allocation_status = Payment.Allocation.MANUAL_REVIEW
        payment.save(update_fields=["allocation_status", "updated_at"])
        return
    try:
        from apps.finance.services import allocate_payment
    except Exception:
        # Lane A not present in this schema yet — leave allocation for the
        # manual endpoint; the payment itself is completed and signalled.
        payment.allocation_status = Payment.Allocation.MANUAL_REVIEW
        payment.save(update_fields=["allocation_status", "updated_at"])
        return
    # The payment is REAL MONEY regardless of whether finance can auto-match it
    # to an open invoice. A duplicate/late charge against an already-PAID invoice
    # (or an over-allocation) makes allocate_payment raise ValidationException;
    # that must NOT roll back the completion (signal + fiscalization). Wrap the
    # allocation in a SAVEPOINT so its failure only rolls back the allocation,
    # and defer to the manual-review endpoint (as the docstring promises).
    try:
        with transaction.atomic():
            allocate_payment(
                payment_id=payment.pk, amount_uzs=payment.amount_uzs, invoice_ids=[int(invoice_id)]
            )
    except ValidationException:
        payment.allocation_status = Payment.Allocation.MANUAL_REVIEW
        payment.save(update_fields=["allocation_status", "updated_at"])
        return
    payment.allocation_status = Payment.Allocation.ALLOCATED
    payment.save(update_fields=["allocation_status", "updated_at"])


@transaction.atomic
def allocate_manual(*, payment_id: int, allocations: list[dict[str, Any]]) -> Payment:
    """Manual allocation endpoint body — applies each ``(invoice, amount)`` line to the
    invoice the operator named.

    Uses Lane A's ``allocate_payment_lines`` so the per-line amounts are honored. The
    previous implementation looped ``allocate_payment`` (a total oldest-due-first split
    that is idempotent per payment): every line after the first hit that idempotency
    guard and was silently dropped, yet the payment was still marked ALLOCATED — losing
    money. Guards the total against the real amount received so an operator cannot
    allocate more than the payment is worth."""
    payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
    if payment is None:
        raise NotFoundException(_("Payment not found."), code="payment_not_found")
    if payment.status != Payment.Status.COMPLETED:
        raise UnprocessableEntity(_("Only completed payments can be allocated."))
    if not allocations:
        raise ValidationException(
            _("At least one allocation line is required."), code="no_allocations"
        )
    total = sum(
        (Decimal(str(a["amount"])) for a in allocations), Decimal("0")
    ).quantize(Decimal("0.01"))
    if total > payment.amount_uzs:
        raise UnprocessableEntity(
            _("Allocations exceed the payment amount."), code="over_allocation"
        )
    from apps.finance.services import allocate_payment_lines

    allocate_payment_lines(
        payment_id=payment.pk,
        lines=[
            {"invoice": int(a["invoice"]), "amount": Decimal(str(a["amount"]))}
            for a in allocations
        ],
    )
    payment.allocation_status = Payment.Allocation.ALLOCATED
    payment.save(update_fields=["allocation_status", "updated_at"])
    return payment


# ---------------------------------------------------------------------------
# Cash intake (cashier drawer) — stamps the open CashierShift so the shift
# reconciliation report (_shift_cash_total) reflects real cash taken in.
# ---------------------------------------------------------------------------
@transaction.atomic
def create_cash_payment(
    *,
    invoice_id: int,
    cashier,
    amount_uzs: Decimal | None = None,
    idempotency_key: str | None = None,
) -> Payment:
    """Record a CASH payment taken at the drawer.

    Creates a COMPLETED ``Payment(provider=CASH)`` stamped with the cashier's
    currently OPEN ``CashierShift`` (the only write path that sets
    ``Payment.cashier_shift`` — without it the cashier-shift reconciliation report
    always read zero cash). Drives the normal completion chokepoint so the payment
    fiscalizes + auto-allocates against the invoice exactly like a provider
    payment. Idempotent on ``idempotency_key`` (defaults to a stable per-(schema,
    invoice, shift) key so a double-submit at the drawer does not double-charge)."""
    from apps.finance.models import CashierShift, Invoice

    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        raise UnprocessableEntity(_("Invoice not found."), fields={"invoice": ["not_found"]})

    shift = (
        CashierShift.objects.filter(cashier=cashier, status=CashierShift.Status.OPEN)
        .order_by("-opened_at")
        .first()
    )
    if shift is None:
        raise UnprocessableEntity(
            _("You must have an open cashier shift to take cash."), code="no_open_shift"
        )

    amount = Decimal(amount_uzs).quantize(Decimal("0.01")) if amount_uzs is not None else invoice.total_uzs
    if amount <= Decimal("0"):
        raise ValidationException(_("Cash amount must be positive."), fields={"amount_uzs": ["invalid"]})

    key = idempotency_key or stable_hash(f"cash:{current_schema()}:{invoice_id}:{shift.pk}")
    payment, created = get_or_create_payment(
        idempotency_key=key,
        provider=Payment.Method.CASH,
        amount_uzs=amount,
        account_ref=invoice.number,
        payer=getattr(invoice.student, "user", None),
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id},
    )
    if created or payment.cashier_shift_id is None:
        payment.cashier_shift = shift
        payment.save(update_fields=["cashier_shift", "updated_at"])
    mark_payment_completed(payment_id=payment.pk, provider_txn_id=f"cash:{shift.pk}")
    payment.refresh_from_db()
    return payment


# ---------------------------------------------------------------------------
# Checkout (D3-B-7)
# ---------------------------------------------------------------------------
@transaction.atomic
def create_checkout(*, invoice_id: int, provider: str, idempotency_key: str, payer=None) -> dict[str, Any]:
    """Create (or fetch) a pending Payment for an invoice and return the client's
    redirect/payload. Idempotent on ``idempotency_key`` (TASKS §16)."""
    if provider not in Provider.values:
        raise ValidationException(_("Unknown provider."), fields={"provider": ["invalid"]})
    from apps.finance.models import Invoice

    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        raise UnprocessableEntity(_("Invoice not found."), fields={"invoice": ["not_found"]})

    payment, _created = get_or_create_payment(
        idempotency_key=idempotency_key,
        provider=provider,
        amount_uzs=invoice.total_uzs,
        account_ref=invoice.number,
        payer=payer,
        metadata={"invoice_id": invoice_id, "student_id": invoice.student_id},
    )

    config = ProviderConfig.objects.filter(provider=provider, is_active=True).first()
    account = {"invoice": invoice.number}
    payload = _build_provider_checkout(provider=provider, payment=payment, config=config, account=account)
    return {"payment_id": payment.pk, "provider": provider, **payload}


def _build_provider_checkout(*, provider: str, payment: Payment, config, account: dict) -> dict[str, Any]:
    # Click/Uzum transmit the order amount in whole soum (UZS). Round half-up to
    # the nearest soum rather than truncating with int() — a bare int() silently
    # drops fractional UZS (e.g. 149999.99 -> 149999), short-changing the bill.
    # Payme uses tiyin (1 UZS = 100 tiyin) so it carries the exact cents.
    amount_uzs = int(payment.amount_uzs.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    # The merchant reference we hand the provider is echoed back verbatim on the
    # completion callback, where the webhook resolves the invoice by
    # ``Invoice.number`` (click_webhook_view / uzum_webhook_view). It MUST therefore
    # be the invoice number, not the Payment PK — sending the PK made every real
    # Click/Uzum callback resolve to a non-existent invoice (number="<pk>") and the
    # payment was acknowledged to the provider yet never credited. ``account`` carries
    # the canonical reference (``{"invoice": invoice.number}``), matching Payme.
    merchant_ref = str(account["invoice"])
    if provider == Provider.CLICK:
        from infrastructure.payments.click import get_click_client

        return get_click_client().build_checkout(
            amount_uzs=amount_uzs, merchant_trans_id=merchant_ref, config=config
        )
    if provider == Provider.PAYME:
        from infrastructure.payments.payme import get_payme_client

        return get_payme_client().build_checkout(
            amount_tiyin=int(payment.amount_uzs * _TIYIN), account=account, config=config
        )
    if provider == Provider.UZUM:
        from infrastructure.payments.uzum import get_uzum_client

        return get_uzum_client().build_checkout(
            amount_uzs=amount_uzs, order_id=merchant_ref, config=config
        )
    return {}


# ---------------------------------------------------------------------------
# Webhook intake + replay protection (D3-B-5, D3-B-6)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_webhook_event(
    *,
    provider: str,
    event_id: str,
    payload: dict,
    remote_ip: str | None,
    signature_valid: bool,
    idempotent_retry: bool = False,
) -> tuple[WebhookEvent, bool]:
    """Insert a WebhookEvent. Returns ``(event, is_new)``. A replayed
    ``(provider, event_id)`` returns the existing row marked ``duplicate`` and
    ``is_new=False`` — the caller must NOT re-run side effects (D3-B-6).

    ``idempotent_retry=True`` is for protocols whose repeat of the same id is an
    EXPECTED retry rather than a nonce-replay attack: Payme's CreateTransaction is
    idempotent on ``params.id`` (the client echoes the existing transaction), so a
    re-send must NOT be flagged ``duplicate`` (that label is the audit signal for a
    reused nonce). The existing row is returned untouched with ``is_new=False``."""
    existing = WebhookEvent.objects.filter(provider=provider, event_id=event_id).first()
    if existing is not None:
        # A previously REJECTED event is NOT a dedupe winner (mark_webhook_rejected's
        # contract): the provider's corrected retry must be reprocessed, not
        # swallowed as `duplicate`. Re-arm it as a fresh attempt when the retry now
        # carries a valid signature; a still-invalid retry stays rejected.
        if existing.status == WebhookEvent.Status.REJECTED:
            if signature_valid:
                existing.payload = payload
                existing.remote_ip = remote_ip or existing.remote_ip
                existing.signature_valid = True
                existing.status = WebhookEvent.Status.RECEIVED
                existing.save(update_fields=["payload", "remote_ip", "signature_valid", "status"])
                return existing, True
            return existing, False
        # P0 fix (D3-F): a replay must be recorded as `duplicate`, not silently
        # returned with its prior status — this is the audit signal that a nonce
        # was reused, and D3-B-6 / the replay tests assert it. Skip this for an
        # id-idempotent protocol retry (Payme), which is not a replay attack.
        if not idempotent_retry and existing.status != WebhookEvent.Status.DUPLICATE:
            existing.status = WebhookEvent.Status.DUPLICATE
            existing.save(update_fields=["status"])
        return existing, False
    status = WebhookEvent.Status.RECEIVED if signature_valid else WebhookEvent.Status.REJECTED
    event = WebhookEvent.objects.create(
        provider=provider,
        event_id=event_id,
        payload=payload,
        remote_ip=remote_ip or None,
        signature_valid=signature_valid,
        status=status,
    )
    return event, True


def mark_webhook_processed(event: WebhookEvent) -> None:
    event.status = WebhookEvent.Status.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["status", "processed_at"])


def mark_webhook_rejected(event: WebhookEvent) -> None:
    """Mark an event rejected after side-effect validation fails (e.g. amount
    mismatch). Distinct from a signature rejection (recorded at intake): the
    signature was valid but the body was not honoured, and the event must NOT be
    treated as a successful dedupe winner so the provider's retry is reprocessed."""
    event.status = WebhookEvent.Status.REJECTED
    event.save(update_fields=["status"])


# ---------------------------------------------------------------------------
# Payme JSON-RPC store (D3-B-3) — the DB side the PaymeClient delegates to
# ---------------------------------------------------------------------------
# The account-field name the merchant configures in the Payme cabinet (e.g.
# ``order_id``, ``invoice``). The webhook builders use ``order_id``; we accept
# any of these and echo the offending field name back in a Payme ``data`` member.
_PAYME_ACCOUNT_FIELDS = ("order_id", "invoice", "invoice_number", "account")


def _account_field(account: dict[str, Any]) -> tuple[str, str]:
    """Return ``(field_name, value)`` for the account's invoice-number field.

    Tries the known field names in order so a tenant configured with ``order_id``
    or ``invoice`` both resolve; the chosen field name is what a Payme account
    error names in its ``data`` member (DAY-3.md D3-B-3)."""
    for field in _PAYME_ACCOUNT_FIELDS:
        value = account.get(field)
        if value:
            return field, str(value)
    # Nothing usable — name the canonical configured field in `data`.
    return _PAYME_ACCOUNT_FIELDS[0], ""


class PaymeDBStore:
    """Implements ``infrastructure.payments.payme.PaymeStore`` against this
    tenant's Payment/Invoice rows. Account errors raise ``PaymeError`` with the
    code in -31050..-31099 and a ``data`` field naming the offender."""

    def find_account(self, account: dict[str, Any]):
        field, invoice_number = _account_field(account)
        if not invoice_number:
            raise PaymeError(ERR_ACCOUNT_NOT_FOUND, _ml("Invoice number is required."), data=field)
        from apps.finance.models import Invoice

        invoice = Invoice.objects.filter(number=invoice_number).first()
        if invoice is None:
            raise PaymeError(ERR_ACCOUNT_NOT_FOUND, _ml("Invoice not found."), data=field)
        return invoice

    def expected_amount_tiyin(self, invoice) -> int:
        return int(invoice.total_uzs * _TIYIN)

    def get_transaction(self, payme_id: str):
        return Payment.objects.filter(provider=Provider.PAYME, provider_txn_id=payme_id).first()

    @transaction.atomic
    def create_transaction(self, *, payme_id: str, time_ms: int, amount_tiyin: int, account: dict, invoice):
        field, account_value = _account_field(account)
        # One open transaction per account: another open/performed Payme txn for
        # the same invoice → -31099 (account already paid/locked).
        conflicting = (
            Payment.objects.filter(provider=Provider.PAYME, account_ref=account_value)
            .exclude(provider_txn_id=payme_id)
            .filter(provider_state__in=[STATE_CREATED, STATE_PERFORMED])
            .exists()
        )
        if conflicting:
            from infrastructure.payments.payme import ERR_ACCOUNT_ALREADY_PAID

            raise PaymeError(ERR_ACCOUNT_ALREADY_PAID, _ml("Another transaction is in progress."), data=field)
        key = f"payme:{current_schema()}:{payme_id}"
        payment, _created = get_or_create_payment(
            idempotency_key=key,
            provider=Provider.PAYME,
            amount_uzs=Decimal(amount_tiyin) / _TIYIN,
            account_ref=account_value,
            metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id, "account": account},
        )
        payment.provider_txn_id = payme_id
        payment.provider_state = STATE_CREATED
        payment.provider_created_at_ms = time_ms
        payment.status = Payment.Status.PROCESSING
        payment.save(
            update_fields=[
                "provider_txn_id",
                "provider_state",
                "provider_created_at_ms",
                "status",
                "updated_at",
            ]
        )
        return payment

    @transaction.atomic
    def perform_transaction(self, txn: Payment):
        # Atomic so the PERFORMED state-save and the completion flip commit/roll
        # back together (no orphan PERFORMED-without-completion). Allocation
        # failures are absorbed inside mark_payment_completed's _auto_allocate
        # savepoint and surface as allocation_status=MANUAL_REVIEW — they never
        # raise a ValidationException out of here into the JSON-RPC handler.
        now_ms = int(timezone.now().timestamp() * 1000)
        txn.provider_state = STATE_PERFORMED
        txn.metadata = {**txn.metadata, "perform_time_ms": now_ms}
        txn.save(update_fields=["provider_state", "metadata", "updated_at"])
        mark_payment_completed(payment_id=txn.pk, provider_txn_id=txn.provider_txn_id)
        txn.refresh_from_db()
        return txn

    def cancel_transaction(self, txn: Payment, *, reason: int):
        now_ms = int(timezone.now().timestamp() * 1000)
        was_performed = txn.provider_state == STATE_PERFORMED
        txn.provider_state = STATE_CANCELLED_AFTER_PERFORM if was_performed else STATE_CANCELLED
        txn.cancel_reason = reason
        txn.metadata = {**txn.metadata, "cancel_time_ms": now_ms}
        txn.save(update_fields=["provider_state", "cancel_reason", "metadata", "updated_at"])
        if was_performed:
            # State -2: cancel after perform → drive a finance Refund (D3-B-8).
            _refund_for_cancelled_payment(txn, reason=reason)
            txn.status = Payment.Status.REFUNDED
            txn.save(update_fields=["status", "updated_at"])
        else:
            mark_payment_failed(payment_id=txn.pk, cancel_reason=reason)
            txn.refresh_from_db()
        return txn

    def statement(self, *, frm: int, to: int) -> list[dict[str, Any]]:
        qs = Payment.objects.filter(
            provider=Provider.PAYME, provider_created_at_ms__gte=frm, provider_created_at_ms__lte=to
        ).order_by("provider_created_at_ms")
        return [
            {
                "id": p.provider_txn_id,
                "time": p.provider_created_at_ms,
                "amount": int(p.amount_uzs * _TIYIN),
                "account": p.metadata.get("account", {}),
                "create_time": p.create_time_ms,
                "perform_time": p.perform_time_ms,
                "cancel_time": p.cancel_time_ms,
                "transaction": p.provider_txn_id,
                "state": p.provider_state,
                "reason": p.cancel_reason,
            }
            for p in qs
        ]


def _ml(text: str) -> dict[str, str]:
    """Payme localized message triplet."""
    return {"ru": text, "uz": text, "en": text}


# ---------------------------------------------------------------------------
# Refund flow (D3-B-8) — drives finance.Refund via lazy import
# ---------------------------------------------------------------------------
def _refund_for_cancelled_payment(payment: Payment, *, reason: int) -> None:
    invoice_id = payment.metadata.get("invoice_id")
    if not invoice_id:
        return
    try:
        from apps.finance.models import Invoice
        from apps.finance.services import register_refund_completion, request_refund
    except Exception:
        return  # finance not present yet — refund recorded only on the payment row
    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return
    refund = request_refund(
        invoice=invoice,
        payment_id=payment.pk,
        amount_uzs=payment.amount_uzs,
        reason=f"payme_cancel:{reason}",
    )
    register_refund_completion(refund_id=refund.pk, payment_id=payment.pk)


@transaction.atomic
def refund_payment(*, payment_id: int, amount_uzs: Decimal | None = None, reason: str = "") -> Payment:
    """Refund a completed payment — drives Lane A's Refund state machine."""
    payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
    if payment is None:
        raise NotFoundException(_("Payment not found."), code="payment_not_found")
    if payment.status != Payment.Status.COMPLETED:
        raise UnprocessableEntity(_("Only completed payments can be refunded."))
    invoice_id = payment.metadata.get("invoice_id")
    if not invoice_id:
        raise UnprocessableEntity(_("Payment is not linked to an invoice."))
    from apps.finance.models import Invoice
    from apps.finance.services import register_refund_completion, request_refund

    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        raise UnprocessableEntity(_("Linked invoice not found."))
    refund = request_refund(
        invoice=invoice,
        payment_id=payment.pk,
        # Presence-check, NOT truthiness: an OMITTED amount (None) means a full refund,
        # but an EXPLICIT 0 must fall through to request_refund's positivity guard (400
        # invalid_amount), not silently become the full amount. `Decimal("0") or X` -> X
        # would turn a "refund nothing" request into a full money-out refund.
        amount_uzs=payment.amount_uzs if amount_uzs is None else amount_uzs,
        reason=reason or "manual_refund",
    )
    register_refund_completion(refund_id=refund.pk, payment_id=payment.pk)
    # Only mark the payment fully REFUNDED once cumulative completed refunds cover
    # its amount. A PARTIAL refund must leave it COMPLETED so a follow-up partial
    # refund is still possible (refund_payment requires status==COMPLETED); the
    # per-payment ceiling in request_refund prevents over-refunding.
    from apps.finance.services import completed_refund_total_for_payment

    if completed_refund_total_for_payment(payment.pk) >= payment.amount_uzs:
        payment.status = Payment.Status.REFUNDED
        payment.save(update_fields=["status", "updated_at"])
    return payment


# ---------------------------------------------------------------------------
# Click / Uzum webhook processing (D3-B-2, D3-B-4)
# ---------------------------------------------------------------------------
def _assert_provider_amount(payload: dict, invoice) -> None:
    """Reject a provider Complete callback whose reported amount does not match the
    invoice total. Payme guards this in its client (-31001 ERR_INVALID_AMOUNT);
    Click/Uzum carry the (signed) amount in the body but the handler never checked
    it, so a partial/forged-but-validly-signed amount would credit the FULL invoice
    and auto-allocate the full total. Compare in whole soum (UZS) — both providers
    transmit the order amount in soum."""
    raw = payload.get("amount")
    if raw is None:
        raise ValidationException(
            _("Provider callback is missing the payment amount."),
            code="amount_missing",
            fields={"amount": ["required"]},
        )
    try:
        reported = Decimal(str(raw))
    except (ArithmeticError, ValueError) as exc:
        raise ValidationException(
            _("Provider callback amount is not a number."),
            code="amount_invalid",
            fields={"amount": [str(raw)]},
        ) from exc
    if not reported.is_finite():  # NaN / Infinity never equal a real total — reject
        raise ValidationException(
            _("Provider callback amount is not a number."),
            code="amount_invalid",
            fields={"amount": [str(raw)]},
        )
    # Click/Uzum are CHARGED in whole soum — the checkout rounds the invoice total
    # half-up to the nearest soum (see _build_provider_checkout), so the completion
    # callback reports that rounded figure, not the raw fractional total. Compare
    # against the SAME rounding: an exact compare against a fractional total_uzs
    # (e.g. 149999.99 from a percentage discount) can NEVER match the whole-soum
    # 150000 the provider charged, making the invoice permanently unpayable online.
    # A whole-soum total is unaffected (quantize is a no-op) and under-payment is
    # still caught (the provider must report the rounded charge). Payme carries exact
    # tiyin and guards amounts in its own client (-31001), so it never reaches here.
    expected = invoice.total_uzs.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if reported != expected:
        raise ValidationException(
            _("Provider amount does not match the invoice total."),
            code="amount_mismatch",
            fields={"amount": [str(reported)], "expected": [str(expected)]},
        )


@transaction.atomic
def process_click_complete(*, payload: dict, invoice) -> Payment:
    """Click action=1 (complete) → completed Payment for the invoice. Rejects a
    callback whose reported amount != invoice.total_uzs (amount integrity)."""
    _assert_provider_amount(payload, invoice)
    click_trans_id = str(payload.get("click_trans_id", ""))
    key = f"click:{current_schema()}:{click_trans_id}"
    payment, _created = get_or_create_payment(
        idempotency_key=key,
        provider=Provider.CLICK,
        amount_uzs=invoice.total_uzs,
        account_ref=invoice.number,
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id},
    )
    payment.provider_txn_id = click_trans_id
    payment.save(update_fields=["provider_txn_id", "updated_at"])
    mark_payment_completed(payment_id=payment.pk, provider_txn_id=click_trans_id)
    payment.refresh_from_db()
    return payment


@transaction.atomic
def process_uzum_payment(*, payload: dict, invoice) -> Payment:
    """Uzum Complete → completed Payment. Rejects a callback whose reported amount
    != invoice.total_uzs (amount integrity, mirroring Payme's -31001 guard)."""
    _assert_provider_amount(payload, invoice)
    txn_id = str(payload.get("transaction_id") or payload.get("event_id") or payload.get("order_id", ""))
    key = f"uzum:{current_schema()}:{txn_id}"
    payment, _created = get_or_create_payment(
        idempotency_key=key,
        provider=Provider.UZUM,
        amount_uzs=invoice.total_uzs,
        account_ref=invoice.number,
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id},
    )
    payment.provider_txn_id = txn_id
    payment.save(update_fields=["provider_txn_id", "updated_at"])
    mark_payment_completed(payment_id=payment.pk, provider_txn_id=txn_id)
    payment.refresh_from_db()
    return payment


# ---------------------------------------------------------------------------
# Fiscalization task body (D3-B-9) — idempotent
# ---------------------------------------------------------------------------
@transaction.atomic
def fiscalize_payment_body(payment_id: int) -> str | None:
    """Idempotent: an existing CONFIRMED FiscalReceipt short-circuits. Returns
    the fiscal sign. Stores the marker on the receipt row so a retry no-ops."""
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    receipt, _created = FiscalReceipt.objects.get_or_create(payment=payment)
    if receipt.status == FiscalReceipt.Status.CONFIRMED:
        return receipt.fiscal_sign
    if payment.status != Payment.Status.COMPLETED:
        raise UnprocessableEntity(_("Only completed payments are fiscalized."))

    from infrastructure.fiscal import get_fiscal_client

    key = stable_hash(f"fiscal:{current_schema()}:{payment.pk}")
    receipt.status = FiscalReceipt.Status.SUBMITTED
    receipt.attempts = receipt.attempts + 1
    receipt.submitted_at = timezone.now()
    receipt.save(update_fields=["status", "attempts", "submitted_at", "updated_at"])

    result = get_fiscal_client().fiscalize(
        payment_id=payment.pk,
        amount_uzs=str(payment.amount_uzs),
        items=[{"name": payment.account_ref or "payment", "amount": str(payment.amount_uzs), "qty": 1}],
        idempotency_key=key,
    )
    receipt.fiscal_sign = result["fiscal_sign"]
    receipt.qr_url = result["qr_url"]
    receipt.payload = result.get("raw", {})
    receipt.status = FiscalReceipt.Status.CONFIRMED
    receipt.confirmed_at = timezone.now()
    receipt.save(update_fields=["fiscal_sign", "qr_url", "payload", "status", "confirmed_at", "updated_at"])
    return receipt.fiscal_sign


def enqueue_receipt_pdf(payment_id: int, schema: str) -> None:
    """Enqueue the off-request receipt-PDF render (TD-14). Called on-demand from
    the receipt endpoint, NOT from fiscalization — so the payment-completion
    chokepoint never couples to weasyprint (absent on the dev box)."""
    from celery_tasks.payment_tasks import generate_receipt_pdf

    generate_receipt_pdf.delay(payment_id, _schema_name=schema)


def mark_fiscal_failed(payment_id: int, exc: Exception) -> None:
    FiscalReceipt.objects.filter(payment_id=payment_id).exclude(status=FiscalReceipt.Status.CONFIRMED).update(
        status=FiscalReceipt.Status.FAILED, payload={"error": str(exc)[:2000]}
    )


# ---------------------------------------------------------------------------
# Receipt PDF (D3-B-10) — weasyprint LAZY, S3 → signed URL (TD-14)
# ---------------------------------------------------------------------------
def _render_receipt_pdf(payment: Payment, receipt: FiscalReceipt) -> bytes:
    """weasyprint is imported lazily so the app loads where its GTK native libs
    are absent (Windows dev box); only this call needs them (mirrors the academics
    transcript renderer)."""
    from django.template.loader import render_to_string
    from django.utils import translation
    from weasyprint import HTML  # lazy on purpose: GTK native libs only needed here

    lang = getattr(getattr(payment.payer, "preferred_language", None), "lower", lambda: "en")()
    if lang not in ("uz", "ru", "en"):
        lang = "en"
    with translation.override(lang):
        html = render_to_string(
            f"documents/receipt_{lang}.html",
            {"payment": payment, "receipt": receipt},
        )
    return HTML(string=html).write_pdf()


@transaction.atomic
def generate_receipt_pdf_body(payment_id: int) -> str | None:
    """Idempotent: returns the existing key if already rendered. Stores the S3 key
    on ``FiscalReceipt.payload['pdf_key']`` so the receipt endpoint can sign it."""
    payment = Payment.objects.select_for_update().select_related("payer").get(pk=payment_id)
    receipt = getattr(payment, "fiscal_receipt", None)
    if receipt is None:
        raise UnprocessableEntity(_("Payment has no fiscal receipt yet."))
    existing = (receipt.payload or {}).get("pdf_key")
    if existing:
        return existing

    from infrastructure.storage.s3_client import upload_bytes

    pdf = _render_receipt_pdf(payment, receipt)
    key = f"{current_schema()}/receipts/{payment.pk}.pdf"
    upload_bytes(key, pdf, content_type="application/pdf")
    receipt.payload = {**(receipt.payload or {}), "pdf_key": key}
    receipt.save(update_fields=["payload", "updated_at"])
    return key


# Re-export for the webhook handler's convenience.
__all__ = [
    "PaymeDBStore",
    "allocate_manual",
    "create_cash_payment",
    "create_checkout",
    "enqueue_receipt_pdf",
    "fiscalize_payment_body",
    "generate_receipt_pdf_body",
    "get_or_create_payment",
    "mark_fiscal_failed",
    "mark_payment_completed",
    "mark_payment_failed",
    "mark_webhook_rejected",
    "process_click_complete",
    "process_uzum_payment",
    "record_webhook_event",
    "refund_payment",
]
