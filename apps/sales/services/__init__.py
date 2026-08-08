"""Sales services (#8): record a cash sale as an immutable money-IN ledger row, and
refund it with a compensating money-OUT row (the ledger is never mutated).

These domain functions are preserved verbatim; the layered service
(services/v1/sale_service.py) wraps them after resolving/scoping in the view.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.approvals.models import LedgerEntry
from apps.sales.models import Sale
from core.exceptions import ConflictException, NotFoundException, UnprocessableEntity, ValidationException
from core.idempotency import (
    assert_principal_actor,
    lock_idempotency_key,
    operation_fingerprint,
    principal_scoped_key_hash,
    validate_idempotency_key,
)
from core.role_principals import STAFF_PRINCIPAL_KINDS, RolePrincipal

_TWO_PLACES = Decimal("0.01")
_MAX_AMOUNT = Decimal("1e16")  # NUMERIC(18,2): at most 16 integer digits


def _party_label(student) -> str:
    name = student.get_full_name() or student.student_id
    return name[:200]


@transaction.atomic
def record_sale(
    *,
    item: str,
    quantity: int,
    unit_price_uzs: Decimal,
    student,
    payment_method_id: int,
    sold_by,
    principal: RolePrincipal,
    idempotency_key: str,
    is_unscoped: bool,
    branch_ids: set[int],
    note: str = "",
) -> Sale:
    """Record or replay one sale without duplicating its money-IN ledger row."""
    from apps.finance.models import PaymentMethod

    assert_principal_actor(
        actor=sold_by,
        principal=principal,
        allowed_kinds=STAFF_PRINCIPAL_KINDS,
    )
    amount = (Decimal(quantity) * unit_price_uzs).quantize(_TWO_PLACES)
    if amount <= 0:
        raise ValidationException(_("A sale total must be positive."), code="sale_amount_positive")
    if amount >= _MAX_AMOUNT:
        raise ValidationException(_("The sale total is too large."), code="sale_amount_too_large")
    clean_note = (note or "")[:255]
    raw_key = validate_idempotency_key(idempotency_key)
    key_hash = principal_scoped_key_hash(namespace="sale-create", principal=principal, raw=raw_key)
    fingerprint = operation_fingerprint(
        namespace="sale-create",
        action="create",
        resource={"student_id": student.pk},
        body={
            "item": item,
            "note": clean_note,
            "payment_method_id": payment_method_id,
            "quantity": quantity,
            "unit_price_uzs": str(unit_price_uzs.quantize(_TWO_PLACES)),
        },
    )
    lock_idempotency_key(namespace="sale-create", principal=principal, key_hash=key_hash)

    existing = Sale.objects.filter(idempotency_key_hash=key_hash).first()
    if existing is not None:
        if (
            existing.sold_by_principal_kind != principal.kind
            or existing.sold_by_principal_id != principal.principal_id
        ):
            raise ConflictException(
                _("This idempotency key belongs to a different sale operation."),
                code="idempotency_key_reused",
            )
        # A retry returns an immutable historical sale payload, so authorize the
        # historical branch stamped on that sale—not the student's mutable current
        # placement. This both preserves a legitimate retry after transfer and
        # prevents a new-branch cashier from receiving old-branch finance data.
        if not is_unscoped and existing.branch_id not in branch_ids:
            raise NotFoundException(code="not_found")
        if existing.operation_fingerprint != fingerprint:
            raise ConflictException(
                _("This idempotency key belongs to a different sale operation."),
                code="idempotency_key_reused",
            )
        # Mutable provider/business state intentionally is not revalidated:
        # deactivating a payment method after success must not break an exact retry.
        return existing

    # Reload under a row lock after the view's cheap lookup. A concurrent transfer
    # may have changed the student's branch while the request was being parsed; the
    # historical money snapshot must use the locked current branch and may never
    # borrow the stale view object.
    from apps.students.models import StudentProfile

    locked_student = (
        StudentProfile.objects.select_for_update()
        .select_related("branch")
        .filter(pk=student.pk)
        .first()
    )
    if locked_student is None or (
        not is_unscoped and locked_student.branch_id not in branch_ids
    ):
        raise NotFoundException(code="not_found")
    student = locked_student

    method = PaymentMethod.objects.filter(pk=payment_method_id, is_active=True).first()
    if method is None:
        raise UnprocessableEntity(_("Unknown or inactive payment method."), code="payment_method_invalid")

    sale = Sale.objects.create(
        item=item,
        quantity=quantity,
        unit_price_uzs=unit_price_uzs,
        amount_uzs=amount,
        student=student,
        branch=student.branch,
        payment_method=method,
        sold_by=sold_by,
        note=clean_note,
    )
    entry = LedgerEntry.objects.create(
        direction=LedgerEntry.Direction.IN,
        entry_type="book_sale",
        amount_uzs=amount,
        branch=student.branch,
        party_label=_party_label(student),
        payment_method=method,
        source_kind="sale",
        source_id=sale.pk,
        note=item[:255],
        created_by=sold_by,
    )
    sale.ledger_entry = entry
    sale.sold_by_principal_kind = principal.kind
    sale.sold_by_principal_id = principal.principal_id
    sale.idempotency_key_hash = key_hash
    sale.operation_fingerprint = fingerprint
    from apps.sales.presenters import sale_to_dict

    sale.creation_response_snapshot = sale_to_dict(sale)
    sale.save(
        update_fields=[
            "ledger_entry",
            "sold_by_principal_kind",
            "sold_by_principal_id",
            "idempotency_key_hash",
            "operation_fingerprint",
            "creation_response_snapshot",
        ]
    )
    return sale


@transaction.atomic
def refund_sale(*, sale_id: int, actor, reason: str = "") -> Sale:
    """Reverse a completed sale: write a compensating money-OUT row (never delete or
    edit the original IN row — the ledger is append-only) and mark the sale refunded.
    Locked + completed-only, so a sale can't be double-refunded."""
    sale = Sale.objects.select_for_update().filter(pk=sale_id).first()
    if sale is None:
        raise NotFoundException(_("Sale not found."), code="sale_not_found")
    if sale.status != Sale.Status.COMPLETED:
        raise UnprocessableEntity(_("Only a completed sale can be refunded."), code="sale_not_refundable")

    entry = LedgerEntry.objects.create(
        direction=LedgerEntry.Direction.OUT,
        entry_type="book_sale_refund",
        amount_uzs=sale.amount_uzs,
        branch=sale.branch,
        # Mirror the original IN row's payee so the paired rows always reconcile, even
        # if the student was renamed after the sale.
        party_label=sale.ledger_entry.party_label if sale.ledger_entry else _party_label(sale.student),
        payment_method=sale.payment_method,
        source_kind="sale_refund",
        source_id=sale.pk,
        note=f"refund: {sale.item}"[:255],
        created_by=actor,
    )
    sale.status = Sale.Status.REFUNDED
    sale.refunded_by = actor
    sale.refunded_at = timezone.now()
    sale.refund_reason = reason
    sale.refund_ledger_entry = entry
    sale.save(update_fields=["status", "refunded_by", "refunded_at", "refund_reason", "refund_ledger_entry"])
    return sale
