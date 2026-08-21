"""Fiscalization adapters (TD-7). Soliq is Uzbekistan's tax authority; every
completed payment must be fiscalized into a signed receipt with a QR URL."""

from infrastructure.fiscal.soliq_client import (
    FiscalClient,
    MockSoliqClient,
    SoliqClient,
    get_fiscal_client,
)

__all__ = ["FiscalClient", "MockSoliqClient", "SoliqClient", "get_fiscal_client"]
