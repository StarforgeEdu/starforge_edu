"""Procurement-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class PurchaseOrderLineDTO:
    description: str
    quantity: Decimal
    unit_price_uzs: Decimal


@dataclass(frozen=True)
class CreatePurchaseOrderDTO:
    """A purchase order to raise. `branch` is resolved (non-archived) + branch-scoped in
    the view; the line items are validated there too (each a well-formed object with a
    positive quantity and a non-negative price) before the domain fn totals them."""

    title: str
    supplier: str
    description: str = ""
    items: list[PurchaseOrderLineDTO] = field(default_factory=list)
