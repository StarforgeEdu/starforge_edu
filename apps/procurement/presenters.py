"""Procurement response presenters (the DRF PurchaseOrderSerializer output shape)."""

from __future__ import annotations

from decimal import Decimal

from apps.procurement.models import PurchaseOrder, PurchaseOrderItem

_TWO_PLACES = Decimal("0.01")


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(_TWO_PLACES))


def po_item_to_dict(item: PurchaseOrderItem) -> dict:
    return {
        "id": item.id,
        "description": item.description,
        "quantity": _money(item.quantity),
        "unit_price_uzs": _money(item.unit_price_uzs),
        "line_total_uzs": _money(item.line_total_uzs),
    }


def po_to_dict(po: PurchaseOrder) -> dict:
    return {
        "id": po.id,
        "request": po.request_id,
        "supplier": po.supplier,
        "branch": po.branch_id,
        "status": po.request.status,
        "amount_uzs": _money(po.request.amount_uzs),
        "items": [po_item_to_dict(i) for i in po.items.all()],
        "created_by": po.created_by_id,
        "created_at": po.created_at.isoformat(),
    }
