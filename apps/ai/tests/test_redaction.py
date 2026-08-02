"""PII redaction round-trip + leakage tests (D4-LA-5)."""

from __future__ import annotations

import pytest

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


def test_long_payment_number_and_lowercase_identity_do_not_leak_fragments():
    text = "Card 8600123412341234, passport ab1234567."
    redacted, mapping = redact(text)
    assert "8600123412341234" not in redacted
    assert "ab1234567" not in redacted.lower()
    assert "4, passport" not in redacted
    assert restore(redacted, mapping) == text


def test_overlapping_names_longest_first():
    # A short name contained in a longer one must not shadow it.
    text = "Ali and Ali Valiyev are different people."
    redacted, mapping = redact(text, known_names=["Ali", "Ali Valiyev"])
    assert "Ali Valiyev" not in redacted
    assert restore(redacted, mapping) == text


def test_names_are_case_insensitive_and_components_do_not_leave_partial_pii():
    text = "ali met VALIYEV after class."
    redacted, mapping = redact(text, known_names=["Ali Valiyev"])
    assert "ali" not in redacted.lower()
    assert "valiyev" not in redacted.lower()
    assert restore(redacted, mapping) == text


def test_attacker_supplied_placeholder_is_never_reused_for_real_pii():
    text = "Literal [STUDENT_1] followed by Ali Valiyev."
    redacted, mapping = redact(text, known_names=["Ali Valiyev"])
    assert mapping.get("[STUDENT_1]") is None
    assert "[STUDENT_2]" in redacted
    assert restore(redacted, mapping) == text


def test_name_matching_cannot_retokenize_a_structured_pii_placeholder():
    text = "Contact ali@example.com."
    redacted, mapping = redact(text, known_names=["[EMAIL_1]"])
    assert redacted == "Contact [EMAIL_1]."
    assert mapping == {"[EMAIL_1]": "ali@example.com"}
    assert restore(redacted, mapping) == text


def test_literal_placeholder_and_email_both_round_trip_without_aliasing():
    text = "Literal [EMAIL_1], contact ali@example.com."
    redacted, mapping = redact(text, known_names=["[EMAIL_1]"])
    assert "[EMAIL_2]" in redacted
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


def test_redaction_rejects_unbounded_or_non_text_name_sets():
    with pytest.raises(ValueError, match="name set"):
        redact("text", known_names=["Ali"] * 257)
    with pytest.raises(ValueError, match="name"):
        redact("text", known_names=[1])  # type: ignore[list-item]


def test_redaction_rejects_unbounded_unique_structured_pii_map():
    values = " ".join(str(10_000_000 + index) for index in range(1025))
    with pytest.raises(ValueError, match="token bound"):
        redact(values)


def test_restore_enforces_expansion_bound_without_partial_output():
    mapping = {"[STUDENT_1]": "A" * 50}
    with pytest.raises(ValueError, match="restored output"):
        restore("[STUDENT_1]" * 3, mapping, max_chars=100)


def test_load_map_rejects_wrong_value_types_and_oversized_maps():
    assert load_map('{"[PHONE_1]": 123}') == {}
    raw = "{" + ",".join(f'"[{index}]":"x"' for index in range(1025)) + "}"
    assert load_map(raw) == {}


def test_restore_token10_not_clobbered_by_token1():
    mapping = {f"[STUDENT_{i}]": f"name{i}" for i in range(1, 12)}
    text = "[STUDENT_1] and [STUDENT_10] and [STUDENT_11]"
    out = restore(text, mapping)
    assert out == "name1 and name10 and name11"
