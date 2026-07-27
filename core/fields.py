"""Field-level encryption at rest (TD-11).

`EncryptedTextField` / `EncryptedCharField` transparently Fernet-encrypt their
value on the way to the database and decrypt on the way back. Used for
`national_id`, `medical_notes`, provider credentials, and Soliq tokens.

The key comes from `settings.FIELD_ENCRYPTION_KEY` (separate from SECRET_KEY,
rotation runbook in docs/). Ciphertext is longer than plaintext, so both fields
store into a TEXT column; `max_length` still validates the *plaintext*.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

logger = logging.getLogger("starforge.crypto")


# CAVEAT: lru_cache pins the Fernet built from the FIRST key read. Rotating
# FIELD_ENCRYPTION_KEY (or override_settings in tests) does NOT take effect
# until a process restart or an explicit `_fernet.cache_clear()` — the
# rotation runbook must include the restart step.
@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
    if not key:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is required for encrypted fields (TD-11).")
    return Fernet(key.encode() if isinstance(key, str) else key)


class _EncryptedMixin:
    def get_prep_value(self, value):
        value = super().get_prep_value(value)  # type: ignore[misc]
        if value is None or value == "":
            return value
        return _fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # Pre-existing plaintext or a key mismatch — surface the raw value
            # rather than crashing reads (documented rotation passthrough; the
            # rotation runbook handles migration). Log it so tampering or a
            # wrong/rotated FIELD_ENCRYPTION_KEY is observable instead of
            # silently serving ciphertext.
            model = getattr(self, "model", None)
            logger.warning(
                "EncryptedField decrypt failed (InvalidToken) on %s.%s — returning the "
                "raw stored value (rotation passthrough). Check FIELD_ENCRYPTION_KEY "
                "and the rotation runbook.",
                model.__name__ if model is not None else "<unbound>",
                getattr(self, "name", None) or "<unknown>",
            )
            return value

    def to_python(self, value):
        return value


class EncryptedTextField(_EncryptedMixin, models.TextField):
    pass


class EncryptedCharField(_EncryptedMixin, models.CharField):
    def db_type(self, connection) -> str:
        # Ciphertext won't fit max_length; store as TEXT (max_length still
        # bounds the plaintext at the validation layer).
        return "text"
