"""PII redaction round-trip + leakage tests (D4-LA-5)."""

from __future__ import annotations

from apps.ai.redaction import dump_map, load_map, redact, restore


def test_round_trip_is_lossless():
    text = "Student Ali Valiyev (+998901234567, ali@example.com, national id AB1234567) submitted late."
    redacted, mapping = redact(text, known_names=["Ali Valiyev"])
    assert restore(redacted, mapping) == text


def test_phone_national_id_email_name_absent_after_redaction():
    text = "Contact Ali Valiyev at +998901234567 or ali@example.com; id AB1234567."
    redacted, _mapping = redact(text, known_names=["Ali Valiyev"])
    assert "+998901234567" not in redacted
    assert "ali@example.com" not in redacted
    assert "AB1234567" not in redacted
    assert "Ali Valiyev" not in redacted
    # Tokens are present instead.
    assert "[PHONE_1]" in redacted
    assert "[EMAIL_1]" in redacted
    assert "[NATIONAL_ID_1]" in redacted
    assert "[STUDENT_1]" in redacted


def test_repeated_value_reuses_one_token():
    text = "+998901234567 then again +998901234567"
    redacted, mapping = redact(text)
    assert redacted.count("[PHONE_1]") == 2
    assert "[PHONE_2]" not in redacted
    assert restore(redacted, mapping) == text


def test_multiple_distinct_phones_get_distinct_tokens():
    text = "A +998901234567 B +998901111111"
    redacted, mapping = redact(text)
    assert "[PHONE_1]" in redacted
    assert "[PHONE_2]" in redacted
    assert restore(redacted, mapping) == text


def test_overlapping_names_longest_first():
    # A short name contained in a longer one must not shadow it.
    text = "Ali and Ali Valiyev are different people."
    redacted, mapping = redact(text, known_names=["Ali", "Ali Valiyev"])
    assert "Ali Valiyev" not in redacted
    assert restore(redacted, mapping) == text


def test_no_pii_leaves_text_unchanged():
    text = "The lesson covered photosynthesis and the water cycle."
    redacted, mapping = redact(text, known_names=[])
    assert redacted == text
    assert mapping == {}


def test_map_serialization_round_trip():
    _, mapping = redact("call +998901234567", known_names=[])
    assert load_map(dump_map(mapping)) == mapping


def test_load_map_tolerates_blank_and_garbage():
    assert load_map("") == {}
    assert load_map("not json") == {}


def test_empty_text():
    redacted, mapping = redact("", known_names=["X"])
    assert redacted == ""
    assert mapping == {}
    assert restore("", mapping) == ""


def test_restore_token10_not_clobbered_by_token1():
    mapping = {f"[STUDENT_{i}]": f"name{i}" for i in range(1, 12)}
    text = "[STUDENT_1] and [STUDENT_10] and [STUDENT_11]"
    out = restore(text, mapping)
    assert out == "name1 and name10 and name11"
