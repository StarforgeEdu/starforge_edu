"""Spreadsheet-output safety helpers shared by CSV and XLSX exporters."""

from __future__ import annotations

from typing import Any

# Excel and compatible applications interpret these leading characters as a
# formula, even when the source value was ordinary tenant-controlled text.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_cell(value: Any) -> Any:
    """Return spreadsheet-safe text without changing non-string values.

    Prefixing an apostrophe is the portable CSV/XLSX convention for forcing a
    potentially active formula to render as literal text.
    """
    if isinstance(value, str) and value[:1] in FORMULA_PREFIXES:
        return "'" + value
    return value
