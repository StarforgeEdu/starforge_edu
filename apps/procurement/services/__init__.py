"""Procurement services (#15): assemble an itemised purchase order and raise it on
the A-1 engine as a `kind="procurement"` request totalling the line items.

`create_purchase_order` is preserved verbatim; the layered service
(services/v1/purchase_order_service.py) wraps it after the view has validated the line
items and resolved/scoped the branch.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.approvals.services import create_request
from apps.procurement.models import PurchaseOrder, PurchaseOrderItem
from core.exceptions import ValidationException

KIND_PROCUREMENT = "procurement"

_TWO_PLACES = Decimal("0.01")
_MAX_TOTAL = Decimal("1e16")  # NUMERIC(18,2): at most 16 integer digits


@transaction.atomic
def create_purchase_order(
    *, requested_by, supplier: str, title: str, items, description: str = "", branch=None
):
    """Validate the line items, total them, and raise a procurement request whose
    amount is the PO total. Approval + disbursement (to the supplier) then happen
    through the unified approvals queue; the supplier is named on the ledger."""
    if not items:
        raise ValidationException(_("A purchase order needs at least one line item."), code="po_no_items")
    total = Decimal("0")
    clean: list[tuple[str, Decimal, Decimal]] = []
    for item in items:
        # Quantize qty/price to the stored 2dp scale, and total the per-line products
        # ALSO rounded to 2dp, so the grand total equals the sum of the line totals a
        # human sees (the itemisation reconciles exactly — the anti-fraud trail).
        qty = Decimal(str(item["quantity"])).quantize(_TWO_PLACES)
        price = Decimal(str(item["unit_price_uzs"])).quantize(_TWO_PLACES)
        if qty <= 0:
            raise ValidationException(_("Each line item quantity must be positive."), code="po_item_quantity")
        if price < 0:
            raise ValidationException(_("A unit price cannot be negative."), code="po_item_price")
        total += (qty * price).quantize(_TWO_PLACES)
        clean.append((item["description"], qty, price))
    if total <= 0:
        raise ValidationException(_("A purchase order total must be positive."), code="po_total_positive")
    if total >= _MAX_TOTAL:
        raise ValidationException(_("The purchase order total is too large."), code="po_total_too_large")

    req = create_request(
        kind=KIND_PROCUREMENT,
        title=title,
        requested_by=requested_by,
        amount_uzs=total,
        description=description,
        branch=branch,
        # The payee is the SUPPLIER (not the requester) — name them on the ledger.
        payload={"supplier": supplier[:200], "party_label": supplier[:200]},
    )
    po = PurchaseOrder.objects.create(request=req, supplier=supplier, branch=branch, created_by=requested_by)
    PurchaseOrderItem.objects.bulk_create(
        [
            PurchaseOrderItem(purchase_order=po, description=desc, quantity=qty, unit_price_uzs=price)
            for (desc, qty, price) in clean
        ]
    )
    return po
