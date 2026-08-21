"""Field-level encryption at rest (TD-11).

`EncryptedTextField` / `EncryptedCharField` transparently Fernet-encrypt their
value on the way to the database and decrypt on the way back. Used for
`national_id`, `medical_notes`, provider credentials, and Soliq tokens.

The key comes from `settings.FIELD_ENCRYPTION_KEY` (separate from SECRET_KEY,
rotation runbook in docs/). Ciphertext is longer than plaintext, so both fields
store into a TEXT column; `max_length` still validates the *plaintext*.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models

logger = logging.getLogger("starforge.crypto")


class EncryptedFieldDecryptionError(RuntimeError):
    """Stored encrypted data could not be authenticated or decrypted.

    Callers must not receive the raw database value: it may be tampered
    ciphertext, data encrypted under the wrong tenant key, or legacy plaintext.
    Migration/rotation tooling must handle those cases explicitly.
    """


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


def _encrypted_field_context(field) -> tuple[str, str]:
    model = getattr(field, "model", None)
    return (
        model.__name__ if model is not None else "<unbound>",
        getattr(field, "name", None) or "<unknown>",
    )


def _decrypt_value(value, *, field) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, AttributeError) as exc:
        # Returning ``value`` here would turn a corrupt row, wrong key, or
        # injected legacy plaintext into API-visible data. Fail closed and keep
        # diagnostics free of both ciphertext and plaintext.
        model_name, field_name = _encrypted_field_context(field)
        logger.error(
            "EncryptedField authentication failed on %s.%s; refusing to expose stored value.",
            model_name,
            field_name,
        )
        raise EncryptedFieldDecryptionError("Encrypted field decryption failed.") from exc


class _EncryptedMixin:
    def get_prep_value(self, value):
        value = super().get_prep_value(value)  # type: ignore[misc]
        if value is None or value == "":
            return value
        return _fernet().encrypt(str(value).encode()).decode()

    def from_db_value(self, value, _expression, _connection):
        if value is None or value == "":
            return value
        return _decrypt_value(value, field=self)

    def to_python(self, value):
        return value


class EncryptedTextField(_EncryptedMixin, models.TextField):
    pass


class EncryptedCharField(_EncryptedMixin, models.CharField):
    def db_type(self, connection) -> str:
        # Ciphertext won't fit max_length; store as TEXT (max_length still
        # bounds the plaintext at the validation layer).
        return "text"


class EncryptedJSONField(models.TextField):
    """Authenticated encrypted JSON stored in a TEXT column.

    Values remain native Python JSON values at the model boundary. The complete
    serialized document is one Fernet token at rest; corrupt tokens and valid
    tokens containing invalid JSON both fail closed without logging payloads.
    """

    description = "Fernet-encrypted JSON"

    def get_prep_value(self, value):
        if value is None:
            return None
        try:
            plaintext = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValidationError("Value must be valid JSON.") from exc
        return _fernet().encrypt(plaintext.encode()).decode()

    def from_db_value(self, value, _expression, _connection):
        if value is None:
            return None
        plaintext = _decrypt_value(value, field=self)
        try:
            return json.loads(plaintext)
        except (json.JSONDecodeError, TypeError) as exc:
            model_name, field_name = _encrypted_field_context(self)
            logger.error(
                "EncryptedField JSON decoding failed on %s.%s; refusing to expose stored value.",
                model_name,
                field_name,
            )
            raise EncryptedFieldDecryptionError("Encrypted field JSON decoding failed.") from exc

    def to_python(self, value):
        if value is None or isinstance(value, (list, dict, int, float, bool)):
            return value
        if value == "":
            return []
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValidationError("Value must be valid JSON.") from exc
        return value

    def formfield(
        self,
        form_class: type[forms.Field] | None = None,
        choices_form_class: type[forms.ChoiceField] | None = None,
        **kwargs: Any,
    ) -> forms.Field | None:
        return super().formfield(
            form_class=form_class or forms.JSONField,
            choices_form_class=choices_form_class,
            **kwargs,
        )
