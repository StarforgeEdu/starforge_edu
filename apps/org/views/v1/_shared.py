"""Shared input guards for the org create views — required fields + slug format
that the DRF ModelSerializers enforced automatically (now explicit off-DRF)."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_slug

from core.exceptions import ValidationException


def require_present(values: dict[str, str]) -> None:
    """400 if any of ``values`` is empty (a create-required field missing/blank)."""
    missing = {name: ["This field is required."] for name, val in values.items() if not val}
    if missing:
        raise ValidationException(
            "Missing required field(s).", code="validation_error", fields=missing
        )


def require_slug(field: str, value: str) -> None:
    """400 unless ``value`` is a valid slug (letters/numbers/underscores/hyphens)."""
    try:
        validate_slug(value)
    except DjangoValidationError:
        raise ValidationException(
            "Enter a valid slug.",
            code="validation_error",
            fields={field: ["Enter a valid 'slug' (letters, numbers, underscores, hyphens)."]},
        ) from None
