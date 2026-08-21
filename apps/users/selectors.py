"""Read-side selectors for users."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model

from core.validators import normalize_phone

if TYPE_CHECKING:
    from apps.users.models import User
else:
    User = get_user_model()


def find_by_identifier(identifier: str) -> User | None:
    if "@" in identifier:
        return User.objects.filter(email__iexact=identifier).first()
    try:
        normalized = normalize_phone(identifier)
    except Exception:
        return None
    return User.objects.filter(phone=normalized).first()
