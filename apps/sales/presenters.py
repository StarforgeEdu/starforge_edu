"""Sales response presenters (the DRF SaleSerializer output shape)."""

from __future__ import annotations

from decimal import Decimal

from apps.sales.models import Sale

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal | None) -> str | None:
    """Render a NUMERIC(18,2) money column as a fixed 2-dp string (DRF DecimalField
    parity), so `"150000.00"` not `150000` or a float."""
    if value is None:
        return None
    return str(Decimal(value).quantize(_TWO_PLACES))


def sale_to_dict(sale: Sale) -> dict:
    return {
        "id": sale.id,
        "item": sale.item,
        "quantity": sale.quantity,
        "unit_price_uzs": _money(sale.unit_price_uzs),
        "amount_uzs": _money(sale.amount_uzs),
        "student": sale.student_id,
        "branch": sale.branch_id,
        "payment_method": sale.payment_method_id,
        "status": sale.status,
        "ledger_entry": sale.ledger_entry_id,
        "refund_ledger_entry": sale.refund_ledger_entry_id,
        "sold_by": sale.sold_by_id,
        "refunded_by": sale.refunded_by_id,
        "refunded_at": sale.refunded_at.isoformat() if sale.refunded_at else None,
        "refund_reason": sale.refund_reason,
        "note": sale.note,
        "created_at": sale.created_at.isoformat(),
    }


def sale_creation_to_dict(sale: Sale) -> dict:
    """Return the immutable result of the sale-creation operation.

    A later refund mutates workflow fields on ``Sale``. An exact response-loss
    retry of POST /sales/ must nevertheless reproduce the original successful
    creation response, not make it look as if the retry performed the refund.
    """

    if isinstance(sale.creation_response_snapshot, dict):
        return dict(sale.creation_response_snapshot)

    # Compatibility for pre-cutover rows, which cannot be reached by a keyed
    # replay but remain renderable in migrations/admin repair tooling.
    payload = sale_to_dict(sale)
    payload.update(
        {
            "status": Sale.Status.COMPLETED,
            "refund_ledger_entry": None,
            "refunded_by": None,
            "refunded_at": None,
            "refund_reason": "",
        }
    )
    return payload
