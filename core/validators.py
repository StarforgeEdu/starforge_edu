"""Reusable field validators."""

from __future__ import annotations

import phonenumbers
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


def validate_phone(value: str) -> None:
    """E.164 validation with Uzbekistan as default region."""

    try:
        parsed = phonenumbers.parse(value, "UZ")
    except phonenumbers.NumberParseException as exc:
        raise ValidationError(_("Invalid phone number.")) from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError(_("Invalid phone number."))


def normalize_phone(value: str) -> str:
    """Return E.164 representation, defaulting region to UZ.

    A syntactically unparseable value raises `ValidationException` (a 400
    envelope) rather than letting `NumberParseException` bubble to a 500 — this
    is the single chokepoint for every create service and the OTP flow.
    """

    try:
        parsed = phonenumbers.parse(value, "UZ")
    except phonenumbers.NumberParseException as exc:
        from core.exceptions import ValidationException

        raise ValidationException(_("Invalid phone number."), code="invalid_phone") from exc
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
