"""EncryptedField unit tests (TD-11 / D1-LD-1). No DB — exercises the
get_prep_value/from_db_value pair directly."""

from unittest import mock

import pytest

from core import fields as fields_module
from core.fields import (
    EncryptedCharField,
    EncryptedFieldDecryptionError,
    EncryptedJSONField,
    EncryptedTextField,
)


def test_text_field_round_trip():
    field = EncryptedTextField()
    token = field.get_prep_value("sensitive medical notes")
    assert token != "sensitive medical notes"  # actually encrypted at rest
    assert field.from_db_value(token, None, None) == "sensitive medical notes"


def test_char_field_round_trip():
    field = EncryptedCharField(max_length=64)
    token = field.get_prep_value("AB1234567")
    assert token != "AB1234567"
    assert field.from_db_value(token, None, None) == "AB1234567"


def test_json_field_round_trip_preserves_native_value():
    field = EncryptedJSONField()
    contacts = [{"name": "Ona", "phone": "+998901234567", "verified": True}]
    token = field.get_prep_value(contacts)
    assert token.startswith("gAAAA")
    assert "+998901234567" not in token
    assert field.from_db_value(token, None, None) == contacts


def test_none_and_empty_pass_through():
    field = EncryptedTextField()
    assert field.get_prep_value(None) is None
    assert field.get_prep_value("") == ""
    assert field.from_db_value(None, None, None) is None
    assert field.from_db_value("", None, None) == ""


def test_tampered_token_logs_and_fails_closed():
    field = EncryptedTextField()
    field.name = "medical_notes"  # normally set by contribute_to_class
    with (
        mock.patch.object(fields_module.logger, "error") as logged,
        pytest.raises(EncryptedFieldDecryptionError),
    ):
        field.from_db_value("not-a-fernet-token", None, None)
    logged.assert_called_once()
    assert "medical_notes" in logged.call_args.args  # field context is logged
    assert "not-a-fernet-token" not in repr(logged.call_args)


def test_json_field_tampering_and_invalid_decrypted_json_fail_closed():
    field = EncryptedJSONField()
    field.name = "emergency_contacts"
    invalid_json_token = fields_module._fernet().encrypt(b"not-json").decode()

    with pytest.raises(EncryptedFieldDecryptionError):
        field.from_db_value("not-a-fernet-token", None, None)
    with pytest.raises(EncryptedFieldDecryptionError):
        field.from_db_value(invalid_json_token, None, None)
