"""Grade display rendering per the Center's grading scheme (TD-13).

Pure functions — no DB, no settings access — so the mapping is trivially
unit-testable and the same `value_raw` renders differently only because the
scheme knob changed (DoD #2).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Scheme codes mirror apps.org.models.CenterSettings.GradingScheme.
PERCENTAGE = "percentage"
LETTER = "letter"
GPA = "gpa"

_LETTER_BANDS = (
    (Decimal("90"), "A"),
    (Decimal("80"), "B"),
    (Decimal("70"), "C"),
    (Decimal("60"), "D"),
)


def display_for(value_raw: Decimal, scheme: str) -> str:
    """Render a 0-100 `value_raw` for `scheme`. Fits in `Grade.value_display` (8)."""
    raw = Decimal(value_raw)
    if scheme == LETTER:
        for floor, letter in _LETTER_BANDS:
            if raw >= floor:
                return letter
        return "F"
    if scheme == GPA:
        gpa = (raw / Decimal("25")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return str(gpa)
    # percentage (default): one decimal place, e.g. "92.5"
    return str(raw.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
