"""Loans-domain DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CreateLoanDTO:
    """A loan to raise. `branch` (non-archived) and `borrower` (active staff, defaults to
    the requester) are resolved + validated in the view before the domain fn runs."""

    title: str
    amount_uzs: Decimal
    description: str = ""
