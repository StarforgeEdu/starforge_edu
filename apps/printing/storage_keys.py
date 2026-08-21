"""Record-bound object-key grammar for ad-hoc print uploads."""

from __future__ import annotations

from dataclasses import dataclass

from core.storage_keys import (
    is_lower_hex_upload_id,
    normalized_storage_filename,
    positive_decimal_id,
)


@dataclass(frozen=True, slots=True)
class ParsedPrintUploadKey:
    owner_id: int
    upload_id: str
    filename: str


@dataclass(frozen=True, slots=True)
class ParsedPrintDocumentKey:
    grant_id: int
    filename: str


def pending_print_upload_key(
    *,
    schema: str,
    owner_id: int,
    upload_id: str,
    filename: str,
) -> str:
    safe_filename = normalized_storage_filename(filename)
    if owner_id < 1 or not is_lower_hex_upload_id(upload_id) or safe_filename is None:
        raise ValueError("Invalid print upload key component")
    return f"{schema}/printing/uploads/{owner_id}/{upload_id}/{safe_filename}"


def final_print_document_key(*, schema: str, grant_id: int, filename: str) -> str:
    safe_filename = normalized_storage_filename(filename)
    if grant_id < 1 or safe_filename is None:
        raise ValueError("Invalid print document key component")
    return f"{schema}/printing/documents/{grant_id}/{safe_filename}"


def parse_pending_print_upload_key(value: object, *, schema: str) -> ParsedPrintUploadKey | None:
    if not isinstance(value, str):
        return None
    prefix = f"{schema}/printing/uploads/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 3:
        return None
    owner_id = positive_decimal_id(parts[0])
    filename = normalized_storage_filename(parts[2])
    if owner_id is None or not is_lower_hex_upload_id(parts[1]) or filename is None:
        return None
    return ParsedPrintUploadKey(owner_id=owner_id, upload_id=parts[1], filename=filename)


def parse_final_print_document_key(value: object, *, schema: str) -> ParsedPrintDocumentKey | None:
    if not isinstance(value, str):
        return None
    prefix = f"{schema}/printing/documents/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 2:
        return None
    grant_id = positive_decimal_id(parts[0])
    filename = normalized_storage_filename(parts[1])
    if grant_id is None or filename is None:
        return None
    return ParsedPrintDocumentKey(grant_id=grant_id, filename=filename)
