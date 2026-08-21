"""Sales-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RecordSaleDTO:
    """A cash sale to record. `student` is resolved + branch-scoped in the view before
    the service runs; the payment method id is resolved (active only) in the domain fn
    (unknown/inactive -> 422)."""

    item: str
    quantity: int
    unit_price_uzs: Decimal
    payment_method_id: int
    note: str = ""
