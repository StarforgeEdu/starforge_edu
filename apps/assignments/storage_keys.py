"""Canonical object-key grammar for assignment and submission attachments."""

from __future__ import annotations

from dataclasses import dataclass

from core.storage_keys import (
    is_lower_hex_upload_id,
    normalized_storage_filename,
    positive_decimal_id,
)

_DOMAIN = "assignments"
_TARGETS = {"assignments", "submissions"}


@dataclass(frozen=True)
class PendingAssignmentObject:
    owner_id: int
    upload_id: str
    filename: str


@dataclass(frozen=True)
class FinalAssignmentObject:
    target_kind: str
    target_id: int
    grant_id: int
    filename: str


def pending_attachment_key(*, schema: str, owner_id: int, upload_id: str, filename: str) -> str:
    safe_name = normalized_storage_filename(filename)
    if owner_id < 1 or not is_lower_hex_upload_id(upload_id) or safe_name is None:
        raise ValueError("Invalid assignment upload key component")
    return f"{schema}/{_DOMAIN}/uploads/{owner_id}/{upload_id}/{safe_name}"


def final_attachment_key(
    *,
    schema: str,
    target_kind: str,
    target_id: int,
    grant_id: int,
    filename: str,
) -> str:
    safe_name = normalized_storage_filename(filename)
    if target_kind not in _TARGETS or target_id < 1 or grant_id < 1 or safe_name is None:
        raise ValueError("Invalid assignment attachment key component")
    return f"{schema}/{_DOMAIN}/{target_kind}/{target_id}/{grant_id}/{safe_name}"


def parse_pending_attachment_key(value: object, *, schema: str) -> PendingAssignmentObject | None:
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
    return PendingAssignmentObject(owner_id=owner_id, upload_id=parts[1], filename=safe_name)


def parse_final_attachment_key(value: object, *, schema: str) -> FinalAssignmentObject | None:
    if not isinstance(value, str):
        return None
    prefix = f"{schema}/{_DOMAIN}/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 4 or parts[0] not in _TARGETS:
        return None
    target_id = positive_decimal_id(parts[1])
    grant_id = positive_decimal_id(parts[2])
    safe_name = normalized_storage_filename(parts[3])
    if target_id is None or grant_id is None or safe_name != parts[3]:
        return None
    return FinalAssignmentObject(
        target_kind=parts[0],
        target_id=target_id,
        grant_id=grant_id,
        filename=safe_name,
    )


def parse_legacy_attachment_key(value: object, *, schema: str) -> tuple[str, str] | None:
    """Recognize the pre-hardening server-issued ``uuid/filename`` form."""

    if not isinstance(value, str):
        return None
    prefix = f"{schema}/{_DOMAIN}/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) != 2 or not is_lower_hex_upload_id(parts[0]):
        return None
    safe_name = normalized_storage_filename(parts[1])
    if safe_name != parts[1]:
        return None
    return parts[0], safe_name
