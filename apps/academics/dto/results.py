"""One canonical score/note validator shared by JSON, CSV, and services."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from unicodedata import normalize

SCORE_MAX_DIGITS = 6
SCORE_DECIMAL_PLACES = 2
RESULT_NOTE_MAX_LENGTH = 255
MAX_RESULT_COMPONENTS = 20
COMPONENT_NAME_MAX_LENGTH = 64
_COMPONENTS_UNSET = object()


@dataclass(frozen=True, slots=True)
class ResultValues:
    score: Decimal
    note: str
    # None means the caller omitted this optional field; [] is an explicit clear.
    components: tuple[dict[str, str], ...] | None = None


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
    components: Any = _COMPONENTS_UNSET,
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
    parsed_components = None if components is _COMPONENTS_UNSET else validate_result_components(components)
    return ResultValues(
        score=parsed_score,
        note=parsed_note,
        components=None if parsed_components is None else tuple(parsed_components),
    )


def validate_result_components(raw: Any) -> list[dict[str, str]]:
    """Validate a closed, bounded per-skill assessment breakdown.

    Decimal values are stored as canonical strings inside JSON so evidence never
    loses precision through a binary floating-point round trip.
    """
    if not isinstance(raw, list):
        raise ResultFieldError(
            "components",
            "Components must be an array.",
            code="invalid_components",
        )
    if len(raw) > MAX_RESULT_COMPONENTS:
        raise ResultFieldError(
            "components",
            f"Components may contain at most {MAX_RESULT_COMPONENTS} items.",
            code="too_many_components",
        )

    normalized: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw):
        field = f"components[{index}]"
        if not isinstance(item, dict):
            raise ResultFieldError(field, "Each component must be an object.", code="invalid_component")
        keys = set(item)
        unknown = keys - {"name", "score", "max_score"}
        missing = {"name", "score", "max_score"} - keys
        if unknown:
            raise ResultFieldError(
                field,
                f"Unknown fields: {', '.join(sorted(str(key) for key in unknown))}.",
                code="invalid_component",
            )
        if missing:
            raise ResultFieldError(
                field,
                f"Missing fields: {', '.join(sorted(missing))}.",
                code="invalid_component",
            )

        name = item["name"]
        if not isinstance(name, str):
            raise ResultFieldError(f"{field}.name", "Name must be a string.", code="invalid_component")
        if "\x00" in name:
            raise ResultFieldError(
                f"{field}.name",
                "Name must not contain null characters.",
                code="invalid_component",
            )
        # Collapse Unicode whitespace for stable duplicate detection and display.
        name = normalize("NFKC", " ".join(name.split()))
        if not name:
            raise ResultFieldError(f"{field}.name", "Name may not be blank.", code="invalid_component")
        if len(name) > COMPONENT_NAME_MAX_LENGTH:
            raise ResultFieldError(
                f"{field}.name",
                f"Name may have at most {COMPONENT_NAME_MAX_LENGTH} characters.",
                code="invalid_component",
            )
        name_key = name.casefold()
        if name_key in seen_names:
            raise ResultFieldError(
                f"{field}.name",
                "Component names must be unique.",
                code="duplicate_component",
            )
        seen_names.add(name_key)

        try:
            score = _score_value(item["score"])
        except ResultFieldError as exc:
            raise ResultFieldError(f"{field}.score", exc.message, code=exc.code) from None
        try:
            component_max = _score_value(item["max_score"])
        except ResultFieldError as exc:
            raise ResultFieldError(f"{field}.max_score", exc.message, code=exc.code) from None
        if score < 0:
            raise ResultFieldError(
                f"{field}.score",
                "Score must be nonnegative.",
                code="component_score_out_of_range",
            )
        if component_max <= 0:
            raise ResultFieldError(
                f"{field}.max_score",
                "Maximum score must be greater than zero.",
                code="invalid_component_max",
            )
        if score > component_max:
            raise ResultFieldError(
                f"{field}.score",
                "Score may not exceed the component maximum.",
                code="component_score_out_of_range",
            )
        normalized.append(
            {
                "name": name,
                "score": format(score, "f"),
                "max_score": format(component_max, "f"),
            }
        )
    return normalized


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
