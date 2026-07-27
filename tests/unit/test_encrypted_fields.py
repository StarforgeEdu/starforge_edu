"""EncryptedField unit tests (TD-11 / D1-LD-1). No DB — exercises the
get_prep_value/from_db_value pair directly."""

from unittest import mock

from core import fields as fields_module
from core.fields import EncryptedCharField, EncryptedTextField


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


def test_none_and_empty_pass_through():
    field = EncryptedTextField()
    assert field.get_prep_value(None) is None
    assert field.get_prep_value("") == ""
    assert field.from_db_value(None, None, None) is None
    assert field.from_db_value("", None, None) == ""


def test_tampered_token_logs_warning_and_returns_raw():
    """Rotation passthrough: an undecryptable value is returned as-is, but the
    failure must be observable via a starforge.crypto warning."""
    field = EncryptedTextField()
    field.name = "medical_notes"  # normally set by contribute_to_class
    with mock.patch.object(fields_module.logger, "warning") as warn:
        out = field.from_db_value("not-a-fernet-token", None, None)
    assert out == "not-a-fernet-token"
    warn.assert_called_once()
    assert "medical_notes" in warn.call_args.args  # field context is logged
