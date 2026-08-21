from __future__ import annotations

import unicodedata

import pytest

from apps.assignments.storage_keys import parse_final_attachment_key
from core.attachment_storage import allowed_attachment_mime_types, attachment_content_matches
from core.storage_keys import normalized_storage_filename, positive_decimal_id


@pytest.mark.parametrize(
    "filename",
    [
        "../report.pdf",
        "folder/report.pdf",
        "folder\\report.pdf",
        " report.pdf",
        "report.pdf ",
        "bad\nname.pdf",
        ".",
        "..",
    ],
)
def test_storage_filename_rejects_noncanonical_or_path_like_values(filename):
    assert normalized_storage_filename(filename) is None


def test_storage_filename_requires_canonical_unicode():
    decomposed = unicodedata.normalize("NFD", "café.pdf")
    assert normalized_storage_filename(decomposed) is None
    assert normalized_storage_filename("café.pdf") == "café.pdf"


def test_object_key_ids_cannot_overflow_a_database_bigint():
    assert positive_decimal_id("9223372036854775807") == 9_223_372_036_854_775_807
    assert positive_decimal_id("9223372036854775808") is None
    assert (
        parse_final_attachment_key(
            "tenant/assignments/assignments/9223372036854775808/1/report.pdf",
            schema="tenant",
        )
        is None
    )


def test_only_reviewed_attachment_extensions_have_mime_contracts():
    assert allowed_attachment_mime_types("report.pdf") == frozenset({"application/pdf"})
    assert allowed_attachment_mime_types("voice.m4a") == frozenset({"audio/mp4"})
    assert allowed_attachment_mime_types("archive.zip") == frozenset()


def test_m4a_separates_standard_declared_type_from_reviewed_libmagic_signature():
    assert attachment_content_matches(
        filename="voice.m4a",
        declared="audio/mp4",
        sniffed="audio/x-m4a",
    )
    assert attachment_content_matches(
        filename="voice.m4a",
        declared="audio/mp4",
        sniffed="audio/mp4",
    )
    assert not attachment_content_matches(
        filename="voice.m4a",
        declared="video/mp4",
        sniffed="audio/x-m4a",
    )
    assert not attachment_content_matches(
        filename="voice.mp4",
        declared="video/mp4",
        sniffed="audio/x-m4a",
    )


def test_content_library_uses_the_same_reviewed_m4a_signature_contract():
    from apps.content.services import _sniff_matches

    assert _sniff_matches(sniffed="audio/x-m4a", declared="audio/mp4", ext="m4a")
    assert not _sniff_matches(sniffed="video/mp4", declared="audio/mp4", ext="m4a")
