"""Canonical object-key grammar for private message attachments."""

from __future__ import annotations

from dataclasses import dataclass

from core.storage_keys import (
    is_lower_hex_upload_id,
    normalized_storage_filename,
    positive_decimal_id,
)

_DOMAIN = "messaging"


@dataclass(frozen=True)
class PendingMessageObject:
    owner_id: int
    upload_id: str
    filename: str


@dataclass(frozen=True)
class FinalMessageObject:
    message_id: int
    grant_id: int
    filename: str


def pending_attachment_key(*, schema: str, owner_id: int, upload_id: str, filename: str) -> str:
    safe_name = normalized_storage_filename(filename)
    if owner_id < 1 or not is_lower_hex_upload_id(upload_id) or safe_name is None:
        raise ValueError("Invalid messaging upload key component")
    return f"{schema}/{_DOMAIN}/uploads/{owner_id}/{upload_id}/{safe_name}"


def final_attachment_key(*, schema: str, message_id: int, grant_id: int, filename: str) -> str:
    safe_name = normalized_storage_filename(filename)
    if message_id < 1 or grant_id < 1 or safe_name is None:
        raise ValueError("Invalid message attachment key component")
    return f"{schema}/{_DOMAIN}/messages/{message_id}/{grant_id}/{safe_name}"


def parse_pending_attachment_key(value: object, *, schema: str) -> PendingMessageObject | None:
    if not isinstance(value, str):
        return None
    prefix = f"{schema}/{_DOMAIN}/uploads/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 3:
        return None
    owner_id = positive_decimal_id(parts[0])
    safe_name = normalized_storage_filename(parts[2])
    if owner_id is None or not is_lower_hex_upload_id(parts[1]) or safe_name != parts[2]:
        return None
    return PendingMessageObject(owner_id=owner_id, upload_id=parts[1], filename=safe_name)


def parse_final_attachment_key(value: object, *, schema: str) -> FinalMessageObject | None:
    if not isinstance(value, str):
        return None
    prefix = f"{schema}/{_DOMAIN}/messages/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 3:
        return None
    message_id = positive_decimal_id(parts[0])
    grant_id = positive_decimal_id(parts[1])
    safe_name = normalized_storage_filename(parts[2])
    if message_id is None or grant_id is None or safe_name != parts[2]:
        return None
    return FinalMessageObject(message_id=message_id, grant_id=grant_id, filename=safe_name)


def parse_legacy_attachment_key(value: object, *, schema: str) -> PendingMessageObject | None:
    """Recognize the pre-hardening owner/uuid/filename key form."""

    if not isinstance(value, str):
        return None
    prefix = f"{schema}/{_DOMAIN}/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 3:
        return None
    owner_id = positive_decimal_id(parts[0])
    safe_name = normalized_storage_filename(parts[2])
    if owner_id is None or not is_lower_hex_upload_id(parts[1]) or safe_name != parts[2]:
        return None
    return PendingMessageObject(owner_id=owner_id, upload_id=parts[1], filename=safe_name)
