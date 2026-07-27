"""Custom UserManager: username is the identity, phone/email are contacts.

``generate_username`` derives a unique handle when staff-side services create
accounts without one (from the email local-part, the phone digits, or the
person's name), so every account is loginable from day one.
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Any

from django.contrib.auth.base_user import BaseUserManager

from core.validators import normalize_phone

if TYPE_CHECKING:
    from apps.users.models import User

_USERNAME_SAFE = re.compile(r"[^a-z0-9._-]+")


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def generate_username(self, *candidates: str) -> str:
        """Build a unique username from the first usable candidate string.

        Candidates are slugified to [a-z0-9._-]; collisions get a short random
        suffix. Falls back to ``user-<hex>`` when nothing usable is supplied.
        """
        base = ""
        for candidate in candidates:
            if not candidate:
                continue
            if "@" in candidate:
                candidate = candidate.split("@", 1)[0]
            cleaned = _USERNAME_SAFE.sub(".", candidate.strip().lower()).strip("._-")
            if cleaned:
                base = cleaned[:140]
                break
        if not base:
            base = f"user-{secrets.token_hex(4)}"
        username = base
        while self.model._default_manager.filter(username=username).exists():
            username = f"{base}-{secrets.token_hex(3)}"
        return username

    def _create_user(
        self,
        *,
        username: str | None,
        password: str | None,
        phone: str | None = None,
        email: str | None = None,
        **extra_fields: Any,
    ) -> User:
        if email:
            email = self.normalize_email(email)
        if phone:
            phone = normalize_phone(phone)
        if not username:
            username = self.generate_username(
                email or "",
                (phone or "").lstrip("+"),
                f"{extra_fields.get('first_name', '')}.{extra_fields.get('last_name', '')}",
            )
        user = self.model(
            username=self.model.normalize_username(username),
            phone=phone,
            email=email,
            **extra_fields,
        )
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        phone: str | None = None,
        email: str | None = None,
        **extra_fields: Any,
    ) -> User:
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(
            username=username, password=password, phone=phone, email=email, **extra_fields
        )

    def create_superuser(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        phone: str | None = None,
        email: str | None = None,
        **extra_fields: Any,
    ) -> User:
        extra_fields["is_staff"] = True
        extra_fields["is_superuser"] = True
        extra_fields["is_active"] = True
        return self._create_user(
            username=username, password=password, phone=phone, email=email, **extra_fields
        )
