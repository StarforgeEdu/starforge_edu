from __future__ import annotations

import unicodedata

import pytest

from apps.assignments.storage_keys import parse_final_attachment_key
from core.attachment_storage import allowed_attachment_mime_types
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
    assert allowed_attachment_mime_types("archive.zip") == frozenset()
