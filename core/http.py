"""Request helpers for the layered (plain-Django) view style — parse the JSON body
the way DTOs are built from it. Bad JSON / non-object bodies are a clean 400."""

from __future__ import annotations

import json
from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from typing import Any

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _

from core.exceptions import ValidationException


def read_json(request: HttpRequest) -> dict[str, Any]:
    """The request body as a JSON object (``{}`` when empty). 400 on invalid JSON or a
    non-object body (a list/number/string)."""
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        raise ValidationException(_("Request body must be valid JSON."), code="invalid_json") from None
    if not isinstance(data, dict):
        raise ValidationException(_("Request body must be a JSON object."), code="invalid_json")
    return data


def read_json_array(request: HttpRequest, *, allow_empty: bool = False) -> list[Any]:
    """Parse a top-level JSON array for bounded bulk operations.

    Object DTOs use :func:`read_json`; attendance and assessment batches are
    intentionally arrays. Keeping both parsers in this module lets OpenAPI's
    executable-contract check prove that a declared JSON body is really parsed.
    """
    if not request.body:
        if allow_empty:
            return []
        raise ValidationException(
            _("Request body must be a JSON array."),
            code="invalid_json",
        )
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        raise ValidationException(
            _("Request body must be valid JSON."),
            code="invalid_json",
        ) from None
    if not isinstance(data, list):
        raise ValidationException(
            _("Request body must be a JSON array."),
            code="invalid_json",
        )
    return data


def reject_unknown_fields(
    data: dict[str, Any],
    *,
    allowed: Collection[str],
    message: str = "Request contains unsupported fields.",
) -> None:
    """Reject JSON keys that are not part of an operation's explicit DTO.

    Silently discarding a misspelled mutation field is dangerous: the caller can
    receive a successful response even though the requested change never took
    effect.  Keep this check at the request/DTO boundary so service methods never
    receive ambiguous input.
    """

    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ValidationException(
            _(message),
            code="validation_error",
            fields={field: [_("This field is not supported.")] for field in unknown},
        )


def _bad(name: str, msg: str) -> ValidationException:
    return ValidationException(
        _("%(field)s: %(msg)s") % {"field": name, "msg": msg},
        code="validation_error",
        fields={name: [msg]},
    )


def str_field(data: dict[str, Any], name: str, *, default: str = "", max_length: int | None = None) -> str:
    """A string field, coerced from None to ``default``; a non-string is a 400.

    Rejects NUL (0x00) bytes — psycopg cannot store them and would 500 at bind time —
    and, when ``max_length`` is given, enforces it up-front so an over-long value is a
    clean 400 with the field name rather than a leaked DB DataError."""
    value = data.get(name, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _bad(name, "Must be a string.")
    if "\x00" in value:
        raise _bad(name, "Must not contain NUL bytes.")
    if max_length is not None and len(value) > max_length:
        raise _bad(name, f"Must be at most {max_length} characters.")
    return value


def trimmed_str_field(
    data: dict[str, Any],
    name: str,
    *,
    default: str = "",
    max_length: int | None = None,
    required: bool = False,
) -> str:
    """A DRF-style trimmed string.

    Missing optional input uses ``default``; explicit null is rejected rather than
    silently converted to an empty string. Whitespace is stripped *before* the length
    check, matching ``CharField(trim_whitespace=True)``.
    """
    if name not in data:
        if required:
            raise _bad(name, "This field is required.")
        return default
    value = data[name]
    if value is None:
        raise _bad(name, "May not be null.")
    if not isinstance(value, str):
        raise _bad(name, "Must be a string.")
    if "\x00" in value:
        raise _bad(name, "Must not contain NUL bytes.")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise _bad(name, f"Must be at most {max_length} characters.")
    return value


def int_field(
    data: dict[str, Any],
    name: str,
    *,
    required: bool = False,
    default: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """Parse an integer and optionally enforce inclusive bounds."""
    if name not in data or data[name] is None:
        if required:
            raise _bad(name, "This field is required.")
        return default
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _bad(name, "Must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise _bad(name, "Must be an integer.") from None
    if min_value is not None and parsed < min_value:
        raise _bad(name, f"Must be at least {min_value}.")
    if max_value is not None and parsed > max_value:
        raise _bad(name, f"Must be at most {max_value}.")
    return parsed


def decimal_field(
    data: dict[str, Any], name: str, *, max_digits: int | None = None, decimal_places: int = 2
) -> Decimal | None:
    """A Decimal field (accepts a number or numeric string), or None when absent/blank.

    Rejects NaN/Infinity (``Decimal("NaN")`` parses fine but silently corrupts a money
    column) and, when ``max_digits`` is given, any value whose integer part would
    overflow the column's precision (a leaked DataError 500 otherwise). Also rejects a
    value with MORE than ``decimal_places`` fractional digits (DRF's DecimalField
    ``validate_precision`` returned 400 for this) — otherwise a sub-cent price like
    ``"0.014"`` is silently quantized to ``0.01`` by the column while any amount derived
    from the full-precision input diverges, producing an un-auditable money row."""
    raw = data.get(name)
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise _bad(name, "Must be a number.") from None
    if not value.is_finite():
        raise _bad(name, "Must be a finite number.")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > decimal_places:
        raise _bad(name, f"Ensure that there are no more than {decimal_places} decimal places.")
    if max_digits is not None and abs(value) >= Decimal(10) ** (max_digits - decimal_places):
        raise _bad(name, "Number is too large.")
    return value


def parse_bool(value: Any, name: str) -> bool:
    """Parse one explicit bool value using DRF-compatible string forms."""
    if value is None:
        raise _bad(name, "Must be a boolean.")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "t", "y", "on"):
            return True
        if normalized in ("false", "0", "no", "f", "n", "off"):
            return False
    raise _bad(name, "Must be a boolean.")


def bool_field(data: dict[str, Any], name: str, *, default: bool = False) -> bool:
    """A strict bool field; missing uses ``default``, explicit bad input is 400.

    Silently coercing a typo such as ``"treu"`` to ``False`` can invert an
    activation/publish flag, so both true and false spellings are enumerated by
    :func:`parse_bool` and every other value is rejected.
    """
    if name not in data:
        return default
    return parse_bool(data[name], name)
