"""One canonical score/note validator shared by JSON, CSV, and services."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SCORE_MAX_DIGITS = 6
SCORE_DECIMAL_PLACES = 2
RESULT_NOTE_MAX_LENGTH = 255


@dataclass(frozen=True, slots=True)
class ResultValues:
    score: Decimal
    note: str


class ResultFieldError(ValueError):
    """A transport-neutral validation error for one result field."""

    def __init__(self, field: str, message: str, *, code: str) -> None:
        self.field = field
        self.message = message
        self.code = code
        super().__init__(message)


def validate_result_values(
    *,
    score: Any,
    note: Any = "",
    max_score: Decimal,
) -> ResultValues:
    """Validate exactly what ``ExamResult`` can persist without coercion.

    PostgreSQL ``numeric(6,2)`` would otherwise round excessive precision, while
    ``Decimal`` accepts NaN and infinities. Both behaviors are unacceptable for
    published academic evidence, so every transport uses this function before a
    write and the domain service repeats it at its trust boundary.
    """
    parsed_score = _score_value(score)
    if parsed_score < 0 or parsed_score > max_score:
        raise ResultFieldError(
            "score",
            f"Score must be between 0 and {max_score}.",
            code="score_out_of_range",
        )
    parsed_note = _note_value(note)
    return ResultValues(score=parsed_score, note=parsed_note)


def _score_value(raw: Any) -> Decimal:
    if isinstance(raw, bool) or raw is None or raw == "":
        raise ResultFieldError("score", "Score must be a number.", code="invalid_score")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ResultFieldError("score", "Score must be a number.", code="invalid_score") from None
    if not value.is_finite():
        raise ResultFieldError("score", "Score must be finite.", code="invalid_score")

    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > SCORE_DECIMAL_PLACES:
        raise ResultFieldError(
            "score",
            f"Score may have at most {SCORE_DECIMAL_PLACES} decimal places.",
            code="score_precision",
        )
    integer_limit = Decimal(10) ** (SCORE_MAX_DIGITS - SCORE_DECIMAL_PLACES)
    if abs(value) >= integer_limit:
        raise ResultFieldError(
            "score",
            "Score is too large.",
            code="score_precision",
        )
    return value


def _note_value(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ResultFieldError("note", "Note must be a string.", code="invalid_note")
    value = raw.strip()
    if "\x00" in value:
        raise ResultFieldError("note", "Note must not contain null characters.", code="invalid_note")
    if len(value) > RESULT_NOTE_MAX_LENGTH:
        raise ResultFieldError(
            "note",
            f"Note may have at most {RESULT_NOTE_MAX_LENGTH} characters.",
            code="note_too_long",
        )
    return value
