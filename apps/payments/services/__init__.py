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

import hashlib
import hmac
import json
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.payments.models import (
    FiscalReceipt,
    Payment,
    Provider,
    ProviderConfig,
    WebhookEvent,
)
from apps.payments.signals import payment_completed, payment_failed
from core.exceptions import (
    ConflictException,
    NotFoundException,
    ServiceUnavailableException,
    UnprocessableEntity,
    ValidationException,
)
from core.historical_scope import ATTRIBUTED_SCOPE_STATUSES, ScopeAttributionStatus
from core.utils import current_schema, stable_hash
from infrastructure.payments.payme import (
    ERR_ACCOUNT_ALREADY_PAID,
    ERR_ACCOUNT_NOT_FOUND,
    ERR_CANNOT_CANCEL,
    ERR_CANNOT_PERFORM,
    ERR_INTERNAL,
    ERR_INVALID_AMOUNT,
    STATE_CANCELLED,
    STATE_CANCELLED_AFTER_PERFORM,
    STATE_CREATED,
    STATE_PERFORMED,
    PaymeError,
)

_TIYIN = Decimal("100")
_FISCAL_PROVIDER_FIELDS = frozenset(
    {
        "fiscal_receipt_id",
        "mock",
        "payment_id",
        "receipt_id",
        "terminal_id",
        "timestamp",
    }
)
_FISCAL_PROVIDER_STRING_MAX_LENGTH = 256
_FISCAL_QR_URL_MAX_LENGTH = 200
_FISCAL_QR_HOST_RE = re.compile(
    r"\A(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
_RECEIPT_SCHEMA_RE = re.compile(r"\A[A-Za-z0-9_-]{1,63}\Z")
_PAYMENT_AMOUNT_MAX_EXCLUSIVE = Decimal("10000000000000000")
_PAYME_STATEMENT_MAX_ROWS = 10_000
_WEBHOOK_PROCESSING_LEASE = timedelta(minutes=5)


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
    invoice,
) -> tuple[Payment, bool]:
    """Return ``(payment, created)``. The same idempotency key always returns the
    existing row — never a duplicate (the unique constraint is the backstop).
    """
    if not isinstance(idempotency_key, str) or not idempotency_key or not idempotency_key.strip():
        raise ValidationException(_("idempotency_key is required."), fields={"idempotency_key": ["required"]})
    if (
        idempotency_key != idempotency_key.strip()
        or len(idempotency_key) > 64
        or any(ord(character) < 32 or ord(character) == 127 for character in idempotency_key)
    ):
        raise ValidationException(
            _("idempotency_key is invalid."),
            code="invalid_idempotency_key",
            fields={"idempotency_key": ["invalid"]},
        )
    branch_id, department_id = _invoice_attribution(invoice)
    try:
        normalized_amount = Decimal(amount_uzs).quantize(Decimal("0.01"))
    except ArithmeticError as exc:
        raise ValidationException(
            _("Payment amount is invalid."),
            code="invalid_payment_amount",
            fields={"amount_uzs": ["invalid"]},
        ) from exc
    if (
        not normalized_amount.is_finite()
        or normalized_amount <= 0
        or normalized_amount >= _PAYMENT_AMOUNT_MAX_EXCLUSIVE
    ):
        raise ValidationException(
            _("Payment amount is invalid."),
            code="invalid_payment_amount",
            fields={"amount_uzs": ["invalid"]},
        )
    # QuerySet.get_or_create contains the insert in a savepoint and recovers from
    # the unique-key race by re-reading the winner.  The former check-then-create
    # implementation could surface an IntegrityError when two genuine retries
    # reached an as-yet unseen key at the same time.
    payment, created = Payment.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "provider": provider,
            "amount_uzs": normalized_amount,
            "account_ref": account_ref,
            "payer": payer,
            "metadata": metadata or {},
            "branch_at_payment_id": branch_id,
            "department_at_payment_id": department_id,
            "attribution_status": ScopeAttributionStatus.CAPTURED,
        },
    )
    if not created:
        expected_metadata = metadata if isinstance(metadata, dict) else {}
        recorded_metadata = payment.metadata if isinstance(payment.metadata, dict) else {}
        expected_payer_id = getattr(payer, "pk", None)
        identity_mismatch = any(
            key in expected_metadata and str(recorded_metadata.get(key)) != str(expected_metadata[key])
            for key in ("invoice_id", "student_id")
        )
        if (
            payment.provider != provider
            or payment.amount_uzs != normalized_amount
            or payment.account_ref != account_ref
            or payment.payer_id != expected_payer_id
            or identity_mismatch
            or payment.branch_at_payment_id != branch_id
            or payment.department_at_payment_id != department_id
            or payment.attribution_status not in ATTRIBUTED_SCOPE_STATUSES
        ):
            raise ConflictException(
                _("This idempotency key belongs to a different payment intent."),
                code="idempotency_key_reused",
            )
    return payment, created


def _invoice_attribution(invoice) -> tuple[int, int | None]:
    """Return a reviewed invoice snapshot suitable for a new payment."""
    if (
        invoice is None
        or invoice.branch_at_issue_id is None
        or invoice.attribution_status not in ATTRIBUTED_SCOPE_STATUSES
    ):
        raise ValidationException(
            _("The invoice's historical ownership must be reviewed before payment."),
            code="invoice_attribution_unavailable",
        )
    return invoice.branch_at_issue_id, invoice.department_at_issue_id


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
    if payment.status not in (Payment.Status.PENDING, Payment.Status.PROCESSING):
        raise UnprocessableEntity(
            _("A terminal payment cannot be completed."),
            code="illegal_payment_transition",
        )
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
    # Write the durable work marker before touching the broker. If Redis is
    # unavailable after commit, reconciliation can still discover this receipt.
    if _fiscalization_enabled():
        FiscalReceipt.objects.get_or_create(payment=payment)
        transaction.on_commit(
            lambda: _enqueue_fiscalization(payment.pk, schema),
            robust=True,
        )
    return payment


@transaction.atomic
def mark_payment_failed(*, payment_id: int, cancel_reason: int | None = None) -> Payment:
    """Flip a Payment to failed ONCE (no second signal on re-call)."""
    payment = Payment.objects.select_for_update().get(pk=payment_id)
    if payment.status in (Payment.Status.FAILED, Payment.Status.CANCELLED):
        return payment
    if payment.status in (Payment.Status.COMPLETED, Payment.Status.REFUNDED):
        raise UnprocessableEntity(
            _("A completed payment cannot be failed."),
            code="illegal_payment_transition",
        )
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
    if not _fiscalization_enabled():
        return
    from celery_tasks.payment_tasks import fiscalize_payment

    fiscalize_payment.delay(payment_id, _schema_name=schema)


def _fiscalization_enabled() -> bool:
    """Whether the operator permits external fiscalization traffic.

    This switch deliberately leaves finance and payment accounting available;
    it suppresses only the Soliq outbox and provider call.
    """
    return bool(getattr(settings, "FISCALIZATION_ENABLED", True))


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
    except (ConflictException, ValidationException):
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
        raise ValidationException(_("At least one allocation line is required."), code="no_allocations")
    total = sum((Decimal(str(a["amount"])) for a in allocations), Decimal("0")).quantize(Decimal("0.01"))
    # Once an allocation exists, let the finance domain compare the exact
    # committed line intent before applying fresh-request amount validation.
    # This keeps changed retries on the stable idempotency-conflict contract.
    from apps.finance.models import PaymentAllocation
    from apps.finance.services import allocate_payment_lines

    has_existing = PaymentAllocation.objects.filter(payment_id=payment.pk).exists()
    if not has_existing:
        if total > payment.amount_uzs:
            raise UnprocessableEntity(_("Allocations exceed the payment amount."), code="over_allocation")
        if total < payment.amount_uzs:
            raise UnprocessableEntity(
                _("The complete payment amount must be allocated."),
                code="allocation_total_mismatch",
            )
    allocate_payment_lines(
        payment_id=payment.pk,
        lines=[{"invoice": int(a["invoice"]), "amount": Decimal(str(a["amount"]))} for a in allocations],
    )
    payment.allocation_status = Payment.Allocation.ALLOCATED
    payment.save(update_fields=["allocation_status", "updated_at"])
    return payment


# ---------------------------------------------------------------------------
# Cash intake (cashier drawer) — stamps the open CashierShift so the shift
# reconciliation report (_shift_cash_total) reflects real cash taken in.
# ---------------------------------------------------------------------------
def _cash_idempotent_retry(
    payment: Payment,
    *,
    invoice_id: int,
    cashier_id: int,
    amount_uzs: Decimal | None,
    invoice_scope: tuple[int, int | None],
) -> Payment:
    """Return an exact prior cash intent, reject reuse for a different intent.

    Idempotency keys identify one immutable cashier action.  Silently returning a
    payment for another invoice, amount, provider, or cashier would make the HTTP
    response claim that an action succeeded when it did not.
    """
    recorded_invoice_id = payment.metadata.get("invoice_id")
    shift = payment.cashier_shift if payment.cashier_shift_id is not None else None
    amount_matches = amount_uzs is None or payment.amount_uzs == Decimal(amount_uzs).quantize(Decimal("0.01"))
    if not (
        payment.provider == Payment.Method.CASH
        and str(recorded_invoice_id) == str(invoice_id)
        and shift is not None
        and shift.cashier_id == cashier_id
        and amount_matches
        and payment.branch_at_payment_id == invoice_scope[0]
        and payment.department_at_payment_id == invoice_scope[1]
        and payment.attribution_status in ATTRIBUTED_SCOPE_STATUSES
    ):
        raise ConflictException(
            _("This idempotency key belongs to a different cash payment."),
            code="idempotency_key_reused",
        )
    return payment


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
    invoice, shift) key so a double-submit at the drawer does not double-charge).

    The invoice row is the lock for its allocation balance.  Every supported
    allocation path uses the same lock, so two cashiers cannot both validate
    against the same stale remainder and over-collect it.
    """
    from apps.finance.models import CashierShift, Invoice
    from apps.finance.selectors import OPEN_STATUSES

    invoice = Invoice.objects.select_for_update().select_related("student").filter(pk=invoice_id).first()
    if invoice is None:
        raise UnprocessableEntity(_("Invoice not found."), fields={"invoice": ["not_found"]})
    invoice_scope = _invoice_attribution(invoice)

    # An explicit client key is sufficient to resolve a retry even after the
    # original shift has closed.  This is read-only: avoiding a payment-row lock
    # also preserves the global payment->invoice lock order used by refunds.
    if idempotency_key:
        existing = (
            Payment.objects.select_related("cashier_shift").filter(idempotency_key=idempotency_key).first()
        )
        if existing is not None:
            return _cash_idempotent_retry(
                existing,
                invoice_id=invoice.pk,
                cashier_id=cashier.pk,
                amount_uzs=amount_uzs,
                invoice_scope=invoice_scope,
            )

    shift = (
        CashierShift.objects.select_for_update()
        .filter(cashier=cashier, status=CashierShift.Status.OPEN)
        .order_by("-opened_at")
        .first()
    )
    if shift is None:
        raise UnprocessableEntity(
            _("You must have an open cashier shift to take cash."), code="no_open_shift"
        )
    if invoice_scope[0] != shift.branch_id:
        raise UnprocessableEntity(
            _("The invoice belongs to another cashier branch."),
            code="cashier_branch_mismatch",
        )

    key = idempotency_key or stable_hash(f"cash:{current_schema()}:{invoice_id}:{shift.pk}")
    # The service-level fallback key is derived only after the shift is known.
    # A direct service retry without an explicit key must coalesce as well.
    if not idempotency_key:
        existing = Payment.objects.select_related("cashier_shift").filter(idempotency_key=key).first()
        if existing is not None:
            return _cash_idempotent_retry(
                existing,
                invoice_id=invoice.pk,
                cashier_id=cashier.pk,
                amount_uzs=amount_uzs,
                invoice_scope=invoice_scope,
            )

    # Reading allocations while holding the invoice lock observes the latest
    # committed balance and prevents any cooperating allocation writer from
    # changing it until this receipt has been completed and allocated.
    allocated = sum(invoice.allocations.values_list("amount_uzs", flat=True), Decimal("0"))
    outstanding = max((invoice.total_uzs - allocated).quantize(Decimal("0.01")), Decimal("0.00"))
    amount = Decimal(amount_uzs).quantize(Decimal("0.01")) if amount_uzs is not None else outstanding
    if amount <= Decimal("0"):
        raise ValidationException(_("Cash amount must be positive."), fields={"amount_uzs": ["invalid"]})
    if amount > outstanding:
        raise ValidationException(
            _("Cash amount exceeds the invoice's outstanding balance."),
            code="cash_exceeds_outstanding",
            fields={"amount_uzs": [str(amount)], "outstanding_uzs": [str(outstanding)]},
        )
    if invoice.status not in OPEN_STATUSES:
        raise UnprocessableEntity(
            _("Cash can only be taken for an open invoice."),
            code="invoice_not_open",
        )

    payment, created = get_or_create_payment(
        idempotency_key=key,
        provider=Payment.Method.CASH,
        amount_uzs=amount,
        account_ref=invoice.number,
        payer=getattr(invoice.student, "user", None),
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id},
        invoice=invoice,
    )
    if not created:
        payment = _cash_idempotent_retry(
            Payment.objects.select_related("cashier_shift").get(pk=payment.pk),
            invoice_id=invoice.pk,
            cashier_id=cashier.pk,
            amount_uzs=amount,
            invoice_scope=invoice_scope,
        )
        return payment
    if created or payment.cashier_shift_id is None:
        payment.cashier_shift = shift
        payment.save(update_fields=["cashier_shift", "updated_at"])
    mark_payment_completed(payment_id=payment.pk, provider_txn_id=f"cash:{shift.pk}")
    payment.refresh_from_db()
    return payment


# ---------------------------------------------------------------------------
# Checkout (D3-B-7)
# ---------------------------------------------------------------------------
def _invoice_outstanding_uzs(invoice) -> Decimal:
    allocated = invoice.allocations.aggregate(total=Sum("amount_uzs"))["total"] or Decimal("0")
    return max(
        (invoice.total_uzs - allocated).quantize(Decimal("0.01")),
        Decimal("0.00"),
    )


def _pending_provider_intents(*, invoice, provider: str) -> list[Payment]:
    return list(
        Payment.objects.select_for_update()
        .filter(
            provider=provider,
            account_ref=invoice.number,
            status__in=(Payment.Status.PENDING, Payment.Status.PROCESSING),
        )
        .order_by("created_at", "id")[:2]
    )


def _click_whole_soum_amount(amount_uzs: Decimal) -> Decimal:
    """Return the integer UZS amount Click can actually charge.

    This is intentionally a reconciliation helper, not checkout permission to
    round. New fractional Click checkouts fail before creating a payment intent;
    legacy callbacks still need to record the money the provider actually took.
    """
    amount = Decimal(amount_uzs).quantize(Decimal("0.01"))
    charged = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if charged <= 0:
        raise UnprocessableEntity(
            _("The invoice amount cannot be represented by Click."),
            code="click_amount_precision_unsupported",
        )
    return charged.quantize(Decimal("0.01"))


@transaction.atomic
def create_checkout(*, invoice_id: int, provider: str, idempotency_key: str, payer=None) -> dict[str, Any]:
    """Create (or fetch) a pending Payment for an invoice and return the client's
    redirect/payload. Idempotent on ``idempotency_key`` (TASKS §16)."""
    if provider not in (Provider.CLICK, Provider.PAYME):
        raise ValidationException(_("Unknown provider."), fields={"provider": ["invalid"]})
    from apps.finance.models import Invoice
    from apps.finance.selectors import OPEN_STATUSES

    invoice = Invoice.objects.select_for_update().filter(pk=invoice_id).first()
    if invoice is None:
        raise UnprocessableEntity(_("Invoice not found."), fields={"invoice": ["not_found"]})
    if invoice.status not in OPEN_STATUSES:
        raise UnprocessableEntity(
            _("Online payment is available only for an open invoice."),
            code="invoice_not_open",
        )
    outstanding = _invoice_outstanding_uzs(invoice)
    if outstanding <= 0:
        raise UnprocessableEntity(
            _("The invoice has no outstanding balance."),
            code="invoice_has_no_balance",
        )
    if provider == Provider.CLICK and outstanding != outstanding.quantize(Decimal("1")):
        # Click accepts whole soum only. Rounding would make the provider charge
        # disagree with the Payment, allocation, and fiscal receipt. Fail before
        # creating a pending intent or handing the browser a charge URL.
        raise UnprocessableEntity(
            _("This balance cannot be paid with Click; choose another payment method."),
            code="click_amount_precision_unsupported",
            fields={"outstanding_uzs": [str(outstanding)]},
        )

    config = ProviderConfig.objects.filter(provider=provider, is_active=True).first()
    required_by_provider: dict[str, tuple[str, ...]] = {
        Provider.CLICK: ("click_service_id", "click_merchant_id", "click_secret_key"),
        Provider.PAYME: ("payme_merchant_id", "payme_key"),
    }
    required = required_by_provider[provider]
    if config is None or any(not str(getattr(config, field, "")).strip() for field in required):
        # Validate before creating a pending Payment.  The former order left an
        # authoritative-looking orphan whenever credentials were absent.
        raise ServiceUnavailableException(
            _("This payment method is temporarily unavailable."),
            code="payment_provider_unavailable",
        )

    pending = _pending_provider_intents(invoice=invoice, provider=provider)
    if len(pending) > 1:
        raise ConflictException(
            _("Multiple payment attempts require reconciliation."),
            code="ambiguous_payment_intent",
        )
    if pending and pending[0].idempotency_key != idempotency_key:
        raise ConflictException(
            _("A payment attempt is already in progress for this invoice."),
            code="payment_intent_in_progress",
        )

    payment, _created = get_or_create_payment(
        idempotency_key=idempotency_key,
        provider=provider,
        amount_uzs=outstanding,
        account_ref=invoice.number,
        payer=payer,
        metadata={"invoice_id": invoice_id, "student_id": invoice.student_id},
        invoice=invoice,
    )

    account = {"invoice": invoice.number}
    payload = _build_provider_checkout(provider=provider, payment=payment, config=config, account=account)
    return {"payment_id": payment.pk, "provider": provider, **payload}


def _build_provider_checkout(*, provider: str, payment: Payment, config, account: dict) -> dict[str, Any]:
    # Click transmits only whole soum. create_checkout guarantees the persisted
    # Payment is already integral; do not round again at this final boundary.
    # Payme uses tiyin (1 UZS = 100 tiyin) and carries the exact cents.
    # The merchant reference we hand the provider is echoed back verbatim on the
    # completion callback, where the webhook resolves the invoice by
    # ``Invoice.number`` (click_webhook_view / uzum_webhook_view). It MUST therefore
    # be the invoice number, not the Payment PK — sending the PK made every real
    # Click callback resolve to a non-existent invoice (number="<pk>") and the
    # payment was acknowledged to the provider yet never credited. ``account`` carries
    # the canonical reference (``{"invoice": invoice.number}``), matching Payme.
    merchant_ref = str(account["invoice"])
    if provider == Provider.CLICK:
        from infrastructure.payments.click import get_click_client

        if payment.amount_uzs != payment.amount_uzs.quantize(Decimal("1")):
            raise ConflictException(
                _("This Click payment attempt requires reconciliation."),
                code="payment_intent_amount_mismatch",
            )
        return get_click_client().build_checkout(
            amount_uzs=int(payment.amount_uzs), merchant_trans_id=merchant_ref, config=config
        )
    if provider == Provider.PAYME:
        from infrastructure.payments.payme import get_payme_client

        return get_payme_client().build_checkout(
            amount_tiyin=int(payment.amount_uzs * _TIYIN), account=account, config=config
        )
    raise ServiceUnavailableException(
        _("This payment method is temporarily unavailable."),
        code="payment_provider_unavailable",
    )


# ---------------------------------------------------------------------------
# Webhook intake + replay protection (D3-B-5, D3-B-6)
# ---------------------------------------------------------------------------
@transaction.atomic
def record_webhook_event(
    *,
    provider: str,
    event_id: str,
    payload: dict,
    signature_valid: bool,
    idempotent_retry: bool = False,
) -> tuple[WebhookEvent, bool]:
    """Insert a privacy-minimized replay-ledger row.

    A completed nonce replay returns ``is_new=False`` and is marked duplicate.
    A concurrent request that sees ``received`` also returns ``is_new=False``;
    callers must answer with a retryable provider error rather than acknowledge
    completion. Rejected events can be re-armed by a corrected signed retry.

    ``idempotent_retry=True`` is for protocols whose repeat of the same id is an
    EXPECTED retry rather than a nonce-replay attack: Payme's CreateTransaction is
    idempotent on ``params.id`` (the client echoes the existing transaction), so a
    re-send must NOT be flagged ``duplicate`` (that label is the audit signal for a
    reused nonce). The existing row is returned untouched with ``is_new=False``."""
    if not signature_valid:
        # Invalid callbacks are attacker-controlled input, not auditable provider
        # events. Persisting their arbitrary nonces enables distributed storage
        # exhaustion and lets forged traffic pre-seed the replay keyspace.
        raise ValidationException(
            _("Webhook signature is invalid."),
            code="webhook_signature_invalid",
        )
    if provider not in Provider.values:
        raise ValidationException(_("Webhook provider is invalid."), code="webhook_provider_invalid")
    if (
        not isinstance(event_id, str)
        or not event_id
        or len(event_id) > 128
        or event_id.strip() != event_id
        or any(ord(character) < 32 or ord(character) == 127 for character in event_id)
    ):
        raise ValidationException(_("Webhook event identifier is invalid."), code="webhook_event_invalid")
    fingerprint = webhook_payload_fingerprint(provider=provider, payload=payload)
    audit_payload = {"fingerprint_hmac_sha256": fingerprint}

    # get_or_create contains the unique-key insert in a savepoint and re-reads
    # the winner after a concurrent insert. The former select-then-create path
    # could throw IntegrityError for two simultaneous provider retries.
    existing, created = WebhookEvent.objects.get_or_create(
        provider=provider,
        event_id=event_id,
        defaults={
            "payload": audit_payload,
            "signature_valid": True,
            "status": WebhookEvent.Status.RECEIVED,
        },
    )
    if created:
        return existing, True

    # Serialize decisions about an existing nonce. In particular, only one
    # worker may reclaim an abandoned RECEIVED lease after a process crash.
    existing = WebhookEvent.objects.select_for_update().get(pk=existing.pk)

    recorded_fingerprint = (existing.payload or {}).get("fingerprint_hmac_sha256")
    if recorded_fingerprint and recorded_fingerprint != fingerprint:
        # Reusing a provider event id for a different immutable business intent
        # is not an idempotent retry. Never acknowledge it as already processed.
        raise ConflictException(
            _("The provider event identifier was reused for a different payload."),
            code="webhook_event_conflict",
        )
    # A previously REJECTED event is NOT a dedupe winner (mark_webhook_rejected's
    # contract): the provider's corrected retry of the same immutable intent must
    # be reprocessed, not swallowed as `duplicate`.  The fingerprint comparison
    # above prevents a rejected nonce from being rebound to another invoice or
    # amount. Click's provider `error` is deliberately outside the semantic
    # fingerprint, so a corrected success callback can recover the same intent.
    if existing.status == WebhookEvent.Status.REJECTED:
        existing.payload = audit_payload
        existing.signature_valid = True
        existing.status = WebhookEvent.Status.RECEIVED
        existing.processed_at = None
        existing.last_attempted_at = timezone.now()
        existing.save(
            update_fields=[
                "payload",
                "signature_valid",
                "status",
                "processed_at",
                "last_attempted_at",
            ]
        )
        return existing, True
    if (
        existing.status == WebhookEvent.Status.RECEIVED
        and signature_valid
        and existing.last_attempted_at <= timezone.now() - _WEBHOOK_PROCESSING_LEASE
    ):
        existing.last_attempted_at = timezone.now()
        existing.save(update_fields=["last_attempted_at"])
        return existing, True
    # Only a terminal successful event becomes DUPLICATE. A simultaneous retry
    # that finds RECEIVED is still in flight and must receive a retryable error,
    # never a false success acknowledgement.
    if not idempotent_retry and existing.status == WebhookEvent.Status.PROCESSED:
        existing.status = WebhookEvent.Status.DUPLICATE
        existing.save(update_fields=["status"])
    return existing, False


def webhook_payload_fingerprint(*, provider: str, payload: dict[str, Any]) -> str:
    """Keyed-hash semantic intent without retaining or dictionary-leaking PII."""

    if not isinstance(payload, dict):
        raise ValidationException(_("Webhook payload is invalid."), code="webhook_payload_invalid")
    if provider == Provider.CLICK:
        semantic = {
            key: payload.get(key)
            for key in (
                "click_trans_id",
                "service_id",
                "merchant_trans_id",
                "merchant_prepare_id",
                "amount",
                "action",
            )
            if key in payload
        }
    elif provider == Provider.PAYME:
        raw_params = payload.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        semantic = {
            "method": payload.get("method"),
            "params": {key: params.get(key) for key in ("id", "time", "amount", "account") if key in params},
        }
    elif provider == Provider.UZUM:
        semantic = {key: value for key, value in payload.items() if key != "signature"}
    else:
        raise ValidationException(_("Webhook provider is invalid."), code="webhook_provider_invalid")
    try:
        canonical = json.dumps(
            semantic,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationException(_("Webhook payload is invalid."), code="webhook_payload_invalid") from exc
    return hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def mark_webhook_processed(event: WebhookEvent) -> None:
    event.status = WebhookEvent.Status.PROCESSED
    event.processed_at = timezone.now()
    event.last_attempted_at = event.processed_at
    event.save(update_fields=["status", "processed_at", "last_attempted_at"])


def mark_webhook_rejected(event: WebhookEvent) -> None:
    """Mark an event rejected after side-effect validation fails (e.g. amount
    mismatch). Distinct from a signature rejection (recorded at intake): the
    signature was valid but the body was not honoured, and the event must NOT be
    treated as a successful dedupe winner so a retry of the same immutable
    provider intent is reprocessed."""
    event.status = WebhookEvent.Status.REJECTED
    event.processed_at = None
    event.last_attempted_at = timezone.now()
    event.save(update_fields=["status", "processed_at", "last_attempted_at"])


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
    if not isinstance(account, dict):
        raise PaymeError(ERR_ACCOUNT_NOT_FOUND, _ml("Invoice number is required."), data="order_id")
    for field in _PAYME_ACCOUNT_FIELDS:
        value = account.get(field)
        if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        normalized = str(value)
        if (
            normalized
            and len(normalized) <= 32
            and normalized.strip() == normalized
            and not any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        ):
            return field, normalized
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
        from apps.finance.selectors import OPEN_STATUSES

        if invoice.status not in OPEN_STATUSES:
            if invoice.status == Invoice.Status.PAID:
                raise PaymeError(
                    ERR_ACCOUNT_ALREADY_PAID,
                    _ml("Invoice is already paid."),
                    data=field,
                )
            raise PaymeError(ERR_CANNOT_PERFORM, _ml("Invoice is not open for payment."), data=field)
        return invoice

    def expected_amount_tiyin(self, invoice) -> int:
        candidates = list(
            Payment.objects.filter(
                provider=Provider.PAYME,
                account_ref=invoice.number,
                provider_txn_id="",
                status=Payment.Status.PENDING,
            ).order_by("created_at", "id")[:2]
        )
        if len(candidates) > 1:
            raise PaymeError(ERR_CANNOT_PERFORM, _ml("Payment attempts require reconciliation."))
        amount = candidates[0].amount_uzs if candidates else _invoice_outstanding_uzs(invoice)
        if amount <= 0:
            raise PaymeError(ERR_ACCOUNT_ALREADY_PAID, _ml("Invoice is already paid."))
        return int(amount * _TIYIN)

    def get_transaction(self, payme_id: str):
        return Payment.objects.filter(provider=Provider.PAYME, provider_txn_id=payme_id).first()

    def validate_existing_create(
        self,
        txn: Payment,
        *,
        time_ms: int,
        amount_tiyin: int,
        account: dict[str, Any],
        invoice,
    ) -> None:
        _field, account_value = _account_field(account)
        if (
            txn.provider != Provider.PAYME
            or txn.provider_created_at_ms != time_ms
            or int(txn.amount_uzs * _TIYIN) != amount_tiyin
            or txn.account_ref != account_value
            or str((txn.metadata or {}).get("invoice_id")) != str(invoice.pk)
        ):
            raise PaymeError(ERR_CANNOT_PERFORM, _ml("Transaction intent does not match."))

    @transaction.atomic
    def create_transaction(self, *, payme_id: str, time_ms: int, amount_tiyin: int, account: dict, invoice):
        # Serialize creation per invoice. A plain ``conflicting.exists()`` has a
        # check-then-insert race where two different Payme ids can both observe
        # no open transaction and charge the same invoice. The invoice is the
        # stable lock row shared by both contenders.
        from apps.finance.models import Invoice

        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        expected_amount_tiyin = self.expected_amount_tiyin(invoice)
        if amount_tiyin != expected_amount_tiyin:
            raise PaymeError(ERR_INVALID_AMOUNT, _ml("Incorrect amount."))
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
        try:
            payment = _claim_pending_provider_intent(invoice=invoice, provider=Provider.PAYME)
            if payment is None:
                key = stable_hash(f"payme:{current_schema()}:{payme_id}")
                payment, _created = get_or_create_payment(
                    idempotency_key=key,
                    provider=Provider.PAYME,
                    amount_uzs=Decimal(amount_tiyin) / _TIYIN,
                    account_ref=account_value,
                    metadata={
                        "invoice_id": invoice.pk,
                        "student_id": invoice.student_id,
                        # Retain only the account identifier Payme must echo.
                        "account": {field: account_value},
                    },
                    invoice=invoice,
                )
            else:
                payment.metadata = {
                    **(payment.metadata or {}),
                    "account": {field: account_value},
                }
        except (ConflictException, ValidationException) as exc:
            raise PaymeError(ERR_CANNOT_PERFORM, _ml("Transaction intent does not match.")) from exc
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
                "metadata",
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
        locked = Payment.objects.select_for_update().get(pk=txn.pk)
        if locked.provider_state == STATE_PERFORMED:
            return locked
        if locked.provider_state != STATE_CREATED or locked.status != Payment.Status.PROCESSING:
            raise PaymeError(ERR_CANNOT_PERFORM, _ml("Cannot perform transaction."))
        now_ms = int(timezone.now().timestamp() * 1000)
        locked.provider_state = STATE_PERFORMED
        locked.metadata = {**locked.metadata, "perform_time_ms": now_ms}
        locked.save(update_fields=["provider_state", "metadata", "updated_at"])
        mark_payment_completed(payment_id=locked.pk, provider_txn_id=locked.provider_txn_id)
        locked.refresh_from_db()
        return locked

    @transaction.atomic
    def cancel_transaction(self, txn: Payment, *, reason: int):
        txn = Payment.objects.select_for_update().get(pk=txn.pk)
        if txn.provider_state in (STATE_CANCELLED, STATE_CANCELLED_AFTER_PERFORM):
            return txn
        if txn.provider_state not in (STATE_CREATED, STATE_PERFORMED):
            raise PaymeError(ERR_CANNOT_CANCEL, _ml("Cannot cancel transaction."))
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
        qs = list(
            Payment.objects.filter(
                provider=Provider.PAYME, provider_created_at_ms__gte=frm, provider_created_at_ms__lte=to
            ).order_by("provider_created_at_ms", "id")[: _PAYME_STATEMENT_MAX_ROWS + 1]
        )
        if len(qs) > _PAYME_STATEMENT_MAX_ROWS:
            raise PaymeError(ERR_INTERNAL, _ml("Statement result is too large."))
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
        from apps.finance.models import Invoice, Refund
        from apps.finance.services import register_refund_completion
    except Exception:
        return  # finance not present yet — refund recorded only on the payment row
    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return
    # A prior operator request is not proof of money movement, but the signed
    # Payme cancellation is. Confirm any pending slices first and create only the
    # remainder, so an in-flight request cannot make the real provider callback
    # fail or produce an over-refund.
    in_flight = list(
        Refund.objects.select_for_update()
        .filter(
            invoice=invoice,
            payment_id=payment.pk,
            state__in=(
                Refund.State.REQUESTED,
                Refund.State.APPROVED,
                Refund.State.SENT_TO_PROVIDER,
            ),
        )
        .order_by("created_at", "id")
    )
    remaining = payment.amount_uzs
    confirmation = f"{payment.provider_txn_id}:cancel:{reason}"
    for refund in in_flight:
        register_refund_completion(
            refund_id=refund.pk,
            payment_id=payment.pk,
            provider=Provider.PAYME,
            provider_refund_id=f"{confirmation}:{refund.pk}",
        )
        remaining -= refund.amount_uzs
    if remaining > 0:
        # A signed provider cancellation is evidence that the money moved even
        # when local auto-allocation had fallen into MANUAL_REVIEW. Do not reject
        # the real refund merely because there is no receivable allocation left
        # to reverse; record the whole provider movement and release whatever
        # allocation exists. Operator-initiated requests still use the stricter
        # request_refund ceiling above.
        refund = Refund.objects.create(
            invoice=invoice,
            payment_id=payment.pk,
            amount_uzs=remaining,
            reason=f"payme_cancel:{reason}",
            provider=Provider.PAYME,
        )
        register_refund_completion(
            refund_id=refund.pk,
            payment_id=payment.pk,
            provider=Provider.PAYME,
            provider_refund_id=f"{confirmation}:{refund.pk}",
        )


@transaction.atomic
def refund_payment(
    *,
    payment_id: int,
    amount_uzs: Decimal | None = None,
    reason: str = "",
    requested_by=None,
) -> tuple[Payment, Any]:
    """Request a refund without claiming the provider already returned money.

    The request remains REQUESTED until a signed provider event confirms it;
    local allocations and payment status stay intact in the meantime.
    """
    payment = Payment.objects.select_for_update().filter(pk=payment_id).first()
    if payment is None:
        raise NotFoundException(_("Payment not found."), code="payment_not_found")
    if payment.status != Payment.Status.COMPLETED:
        raise UnprocessableEntity(_("Only completed payments can be refunded."))
    invoice_id = payment.metadata.get("invoice_id")
    if not invoice_id:
        raise UnprocessableEntity(_("Payment is not linked to an invoice."))
    from apps.finance.models import Invoice
    from apps.finance.services import request_refund

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
        requested_by=requested_by,
        provider=payment.provider,
    )
    return payment, refund


# ---------------------------------------------------------------------------
# Click / Uzum webhook processing (D3-B-2, D3-B-4)
# ---------------------------------------------------------------------------
def _assert_provider_amount(
    payload: dict,
    invoice,
    *,
    expected_amount_uzs: Decimal | None = None,
) -> None:
    """Reject a provider callback whose amount does not match its payment intent.

    Without an existing checkout intent, the current outstanding invoice balance
    is authoritative. Payme guards the equivalent tiyin value in its client.
    Click/Uzum carry the (signed) amount in the body but the handler never checked
    it, so a partial/forged-but-validly-signed amount would credit the FULL invoice
    and auto-allocate the full total. Compare in whole soum (UZS) — both providers
    transmit the order amount in soum."""
    from apps.finance.selectors import OPEN_STATUSES

    if invoice.status not in OPEN_STATUSES:
        raise ValidationException(
            _("The invoice is not open for payment."),
            code="invoice_not_payable",
        )
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
    # Click/legacy-Uzum callbacks report whole soum. New Click checkout refuses
    # fractional balances, but this comparison remains rounding-aware so a charge
    # URL issued by an older release can be reconciled honestly on completion.
    # Payme carries exact tiyin and guards amounts in its own client (-31001).
    charge = expected_amount_uzs if expected_amount_uzs is not None else _invoice_outstanding_uzs(invoice)
    expected = charge.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if expected <= 0:
        raise ValidationException(
            _("The invoice has no outstanding balance."),
            code="invoice_not_payable",
        )
    if reported != expected:
        raise ValidationException(
            _("Provider amount does not match the invoice total."),
            code="amount_mismatch",
            fields={"amount": [str(reported)], "expected": [str(expected)]},
        )


def validate_provider_callback_amount(*, payload: dict, invoice) -> None:
    """Validate a signed Click prepare callback without creating a payment."""

    outstanding = _invoice_outstanding_uzs(invoice)
    if outstanding != outstanding.quantize(Decimal("1")):
        raise ValidationException(
            _("This balance cannot be paid with Click."),
            code="click_amount_precision_unsupported",
            fields={"amount": [str(outstanding)]},
        )
    _assert_provider_amount(payload, invoice, expected_amount_uzs=outstanding)


def _claim_pending_provider_intent(*, invoice, provider: str) -> Payment | None:
    candidates = list(
        Payment.objects.select_for_update()
        .filter(
            provider=provider,
            account_ref=invoice.number,
            provider_txn_id="",
            status=Payment.Status.PENDING,
        )
        .order_by("created_at", "id")[:2]
    )
    if len(candidates) > 1:
        raise ValidationException(
            _("Multiple payment attempts require reconciliation."),
            code="ambiguous_payment_intent",
        )
    if not candidates:
        return None
    payment = candidates[0]
    expected_branch, expected_department = _invoice_attribution(invoice)
    if (
        str((payment.metadata or {}).get("invoice_id")) != str(invoice.pk)
        or payment.branch_at_payment_id != expected_branch
        or payment.department_at_payment_id != expected_department
        or payment.amount_uzs <= 0
    ):
        raise ValidationException(
            _("The payment attempt does not match this invoice."),
            code="payment_intent_mismatch",
        )
    return payment


def _provider_transaction_id(raw: Any) -> str:
    if raw is None or isinstance(raw, bool):
        raise ValidationException(
            _("Provider callback is missing its transaction identifier."),
            code="transaction_id_invalid",
        )
    value = str(raw)
    if (
        not value
        or len(value) > 64
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationException(
            _("Provider callback transaction identifier is invalid."),
            code="transaction_id_invalid",
        )
    return value


@transaction.atomic
def process_click_complete(*, payload: dict, invoice) -> Payment:
    """Complete Click using the amount the provider actually charged.

    Current checkouts are whole-soum only. A callback can nevertheless arrive
    for a fractional intent issued by an older release. In that case the signed
    whole-soum charge replaces the still-pending Payment amount before completion,
    allocation, signalling, and fiscalization. The original invoice amount stays
    in metadata for reconciliation. A rounded-up overpayment deliberately lands
    in manual review because there is no suspense-account model in this version.
    """
    from apps.finance.models import Invoice

    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    payment = _claim_pending_provider_intent(invoice=invoice, provider=Provider.CLICK)
    expected_amount = payment.amount_uzs if payment is not None else _invoice_outstanding_uzs(invoice)
    _assert_provider_amount(payload, invoice, expected_amount_uzs=expected_amount)
    charged_amount = _click_whole_soum_amount(expected_amount)
    click_trans_id = _provider_transaction_id(payload.get("click_trans_id"))
    if payment is None:
        key = stable_hash(f"click:{current_schema()}:{click_trans_id}")
        metadata: dict[str, Any] = {"invoice_id": invoice.pk, "student_id": invoice.student_id}
        if charged_amount != expected_amount:
            metadata["click_invoice_amount_uzs"] = str(expected_amount.quantize(Decimal("0.01")))
        payment, _created = get_or_create_payment(
            idempotency_key=key,
            provider=Provider.CLICK,
            amount_uzs=charged_amount,
            account_ref=invoice.number,
            metadata=metadata,
            invoice=invoice,
        )
    elif payment.amount_uzs != charged_amount:
        metadata = payment.metadata if isinstance(payment.metadata, dict) else {}
        payment.amount_uzs = charged_amount
        payment.metadata = {
            **metadata,
            "click_invoice_amount_uzs": str(expected_amount.quantize(Decimal("0.01"))),
        }
        payment.save(update_fields=["amount_uzs", "metadata", "updated_at"])
    payment.provider_txn_id = click_trans_id
    payment.save(update_fields=["provider_txn_id", "updated_at"])
    mark_payment_completed(payment_id=payment.pk, provider_txn_id=click_trans_id)
    payment.refresh_from_db()
    return payment


@transaction.atomic
def process_uzum_payment(*, payload: dict, invoice) -> Payment:
    """Uzum Complete → completed Payment. Rejects a callback whose reported amount
    != invoice.total_uzs (amount integrity, mirroring Payme's -31001 guard)."""
    from apps.finance.models import Invoice

    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    outstanding = _invoice_outstanding_uzs(invoice)
    _assert_provider_amount(payload, invoice, expected_amount_uzs=outstanding)
    txn_id = _provider_transaction_id(
        payload.get("transaction_id") or payload.get("event_id") or payload.get("order_id")
    )
    key = stable_hash(f"uzum:{current_schema()}:{txn_id}")
    payment, _created = get_or_create_payment(
        idempotency_key=key,
        provider=Provider.UZUM,
        amount_uzs=outstanding,
        account_ref=invoice.number,
        metadata={"invoice_id": invoice.pk, "student_id": invoice.student_id},
        invoice=invoice,
    )
    payment.provider_txn_id = txn_id
    payment.save(update_fields=["provider_txn_id", "updated_at"])
    mark_payment_completed(payment_id=payment.pk, provider_txn_id=txn_id)
    payment.refresh_from_db()
    return payment


# ---------------------------------------------------------------------------
# Fiscalization task body (D3-B-9) — idempotent
# ---------------------------------------------------------------------------
def _invalid_fiscal_response() -> UnprocessableEntity:
    return UnprocessableEntity(
        _("The fiscal provider returned an invalid receipt."),
        code="invalid_fiscal_response",
    )


def _validated_fiscal_sign(result: Any) -> str:
    if not isinstance(result, dict):
        raise _invalid_fiscal_response()
    value = result.get("fiscal_sign")
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise _invalid_fiscal_response()
    return value


def _normalized_hostname(value: str) -> str | None:
    value = value.strip().lower().rstrip(".")
    if not value:
        return None
    try:
        normalized = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return normalized if _FISCAL_QR_HOST_RE.fullmatch(normalized) else None


def _allowed_fiscal_qr_hosts() -> set[str]:
    configured = getattr(settings, "SOLIQ_QR_ALLOWED_HOSTS", ())
    if isinstance(configured, str):
        configured = configured.split(",")
    hosts: set[str] = set()
    for value in configured:
        if not isinstance(value, str):
            continue
        host = _normalized_hostname(value)
        if host is not None:
            hosts.add(host)
    return hosts


def _safe_fiscal_qr_url(value: Any) -> str:
    """Return only an HTTPS URL on an explicitly approved verification host.

    Soliq is an external trust boundary.  Its response must not become an
    arbitrary browser navigation target in an authenticated management UI.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _FISCAL_QR_URL_MAX_LENGTH
        or "\\" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return ""
    host = _normalized_hostname(parsed.hostname or "")
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or host is None
        or host not in _allowed_fiscal_qr_hosts()
    ):
        return ""
    return value


def _safe_provider_payload(raw: Any) -> dict[str, bool | int | str]:
    """Persist a small diagnostic allowlist, never a provider response verbatim."""
    if not isinstance(raw, dict):
        return {}
    sanitized: dict[str, bool | int | str] = {}
    for key in _FISCAL_PROVIDER_FIELDS:
        value = raw.get(key)
        if type(value) is bool:
            sanitized[key] = bool(value)
        elif type(value) is int and -(2**63) <= value < 2**63:
            sanitized[key] = int(value)
        elif (
            type(value) is str
            and len(value) <= _FISCAL_PROVIDER_STRING_MAX_LENGTH
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            sanitized[key] = str(value)
    return sanitized


def fiscalize_payment_body(payment_id: int) -> str | None:
    """Idempotent: an existing CONFIRMED FiscalReceipt short-circuits. Returns
    the fiscal sign. Stores the marker on the receipt row so a retry no-ops."""
    if not _fiscalization_enabled():
        return None

    # Claim under a row lock, then release the transaction before calling Soliq.
    # A slow external request must not pin the payment row or a DB connection.
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment_id)
        receipt, _created = FiscalReceipt.objects.select_for_update().get_or_create(payment=payment)
        if receipt.status == FiscalReceipt.Status.CONFIRMED:
            return receipt.fiscal_sign
        if payment.status not in (Payment.Status.COMPLETED, Payment.Status.REFUNDED):
            raise UnprocessableEntity(_("Only completed payments are fiscalized."))
        if (
            receipt.status == FiscalReceipt.Status.SUBMITTED
            and receipt.submitted_at is not None
            and receipt.submitted_at > timezone.now() - timedelta(minutes=15)
        ):
            return None
        receipt.status = FiscalReceipt.Status.SUBMITTED
        receipt.attempts += 1
        receipt.submitted_at = timezone.now()
        receipt.confirmed_at = None
        receipt.fiscal_sign = ""
        receipt.qr_url = ""
        receipt.provider_payload = {}
        receipt.pdf_key = ""
        receipt.payload = {}
        receipt.save(
            update_fields=[
                "status",
                "attempts",
                "submitted_at",
                "confirmed_at",
                "fiscal_sign",
                "qr_url",
                "provider_payload",
                "pdf_key",
                "payload",
                "updated_at",
            ]
        )
        amount = str(payment.amount_uzs)
        item_name = payment.account_ref or "payment"
        key = stable_hash(f"fiscal:{current_schema()}:{payment.pk}")

    from infrastructure.fiscal import get_fiscal_client

    result = get_fiscal_client().fiscalize(
        payment_id=payment_id,
        amount_uzs=amount,
        items=[{"name": item_name, "amount": amount, "qty": 1}],
        idempotency_key=key,
    )
    fiscal_sign = _validated_fiscal_sign(result)
    qr_url = _safe_fiscal_qr_url(result.get("qr_url"))
    provider_payload = _safe_provider_payload(result.get("raw"))

    with transaction.atomic():
        receipt = FiscalReceipt.objects.select_for_update().get(payment_id=payment_id)
        if receipt.status == FiscalReceipt.Status.CONFIRMED:
            return receipt.fiscal_sign
        receipt.fiscal_sign = fiscal_sign
        receipt.qr_url = qr_url
        receipt.provider_payload = provider_payload
        receipt.payload = {}
        receipt.status = FiscalReceipt.Status.CONFIRMED
        receipt.confirmed_at = timezone.now()
        receipt.save(
            update_fields=[
                "fiscal_sign",
                "qr_url",
                "provider_payload",
                "payload",
                "status",
                "confirmed_at",
                "updated_at",
            ]
        )
        return receipt.fiscal_sign


def enqueue_receipt_pdf(payment_id: int, schema: str) -> None:
    """Enqueue the off-request receipt-PDF render (TD-14). Called on-demand from
    the receipt endpoint, NOT from fiscalization — so the payment-completion
    chokepoint never couples to weasyprint (absent on the dev box)."""
    from celery_tasks.payment_tasks import generate_receipt_pdf

    generate_receipt_pdf.delay(payment_id, _schema_name=schema)


def mark_fiscal_failed(payment_id: int, _exc: Exception) -> None:
    FiscalReceipt.objects.filter(payment_id=payment_id).exclude(status=FiscalReceipt.Status.CONFIRMED).update(
        status=FiscalReceipt.Status.FAILED,
        fiscal_sign="",
        qr_url="",
        provider_payload={},
        pdf_key="",
        payload={},
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


def receipt_pdf_key(payment_id: int, *, schema_name: str | None = None) -> str:
    """Derive the only object key a tenant may sign for a payment receipt."""
    if isinstance(payment_id, bool) or not isinstance(payment_id, int) or payment_id <= 0:
        raise UnprocessableEntity(_("Receipt storage is unavailable."), code="receipt_storage_unavailable")
    schema = schema_name or current_schema()
    if not isinstance(schema, str) or _RECEIPT_SCHEMA_RE.fullmatch(schema) is None:
        raise UnprocessableEntity(_("Receipt storage is unavailable."), code="receipt_storage_unavailable")
    return f"{schema}/receipts/{payment_id}.pdf"


def trusted_receipt_pdf_key(receipt: FiscalReceipt, *, schema_name: str | None = None) -> str | None:
    """Validate receipt state, tenant prefix, payment id, and the complete key."""
    if receipt.status != FiscalReceipt.Status.CONFIRMED:
        return None
    expected = receipt_pdf_key(receipt.payment_id, schema_name=schema_name)
    return expected if receipt.pdf_key == expected else None


def generate_receipt_pdf_body(payment_id: int) -> str | None:
    """Render to a server-derived tenant key; provider/legacy keys are ignored."""
    payment = Payment.objects.select_related("payer", "fiscal_receipt").get(pk=payment_id)
    receipt = getattr(payment, "fiscal_receipt", None)
    if receipt is None:
        raise UnprocessableEntity(_("Payment has no fiscal receipt yet."))
    if receipt.status != FiscalReceipt.Status.CONFIRMED:
        raise UnprocessableEntity(_("The fiscal receipt is not ready yet."), code="receipt_not_ready")
    existing = trusted_receipt_pdf_key(receipt)
    if existing:
        return existing

    from infrastructure.storage.s3_client import upload_bytes

    pdf = _render_receipt_pdf(payment, receipt)
    key = receipt_pdf_key(payment.pk)
    upload_bytes(key, pdf, content_type="application/pdf")
    with transaction.atomic():
        locked = FiscalReceipt.objects.select_for_update().get(payment_id=payment.pk)
        if locked.status != FiscalReceipt.Status.CONFIRMED:
            raise UnprocessableEntity(_("The fiscal receipt is not ready yet."), code="receipt_not_ready")
        existing = trusted_receipt_pdf_key(locked)
        if existing:
            return existing
        locked.pdf_key = key
        locked.payload = {}
        locked.save(update_fields=["pdf_key", "payload", "updated_at"])
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
    "receipt_pdf_key",
    "record_webhook_event",
    "refund_payment",
    "trusted_receipt_pdf_key",
]
