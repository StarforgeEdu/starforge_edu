"""Canonical object-key rules for the content domain.

Object storage is a separate authorization boundary: a database value must not
be treated as proof that an object belongs to a ``LessonFile``.  These helpers
bind final objects to the row primary key and narrowly recognise the temporary
keys issued by :func:`apps.content.services.request_upload`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.content.models import LessonFile

_SAFE_FILENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,254}\Z")
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")
_DERIVED_THUMBNAIL = "_derived/thumbnail.jpg"
_LEGACY_THUMBNAIL = "thumb.jpg"


def is_safe_storage_filename(value: str) -> bool:
    """Return whether *value* is one plain ASCII storage-key segment."""

    return bool(_SAFE_FILENAME.fullmatch(value)) and value not in {".", ".."}


def pending_key(*, schema: str, upload_id: str, filename: str) -> str:
    if not _UPLOAD_ID.fullmatch(upload_id) or not is_safe_storage_filename(filename):
        raise ValueError("Invalid content upload key component")
    # ``tmp`` is an S3 object-key namespace, never a local filesystem path.
    return f"{schema}/tmp/{upload_id}/{filename}"  # nosec B108


def primary_key(*, schema: str, file_id: int, filename: str) -> str:
    if file_id < 1 or not is_safe_storage_filename(filename):
        raise ValueError("Invalid content object key component")
    return f"{schema}/content/{file_id}/{filename}"


def thumbnail_key(*, schema: str, file_id: int) -> str:
    if file_id < 1:
        raise ValueError("Invalid content object identifier")
    return f"{schema}/content/{file_id}/{_DERIVED_THUMBNAIL}"


def trusted_pending_key(value: object, *, schema: str) -> str | None:
    if not isinstance(value, str):
        return None
    # This is an object-store namespace, never a local temporary directory.
    prefix = f"{schema}/tmp/"  # nosec B108
    if not value.startswith(prefix):
        return None
    remainder = value[len(prefix) :]
    parts = remainder.split("/")
    if len(parts) != 2 or not _UPLOAD_ID.fullmatch(parts[0]) or not is_safe_storage_filename(parts[1]):
        return None
    return value


def trusted_primary_key(file: LessonFile, *, schema: str) -> str | None:
    value = file.s3_key
    if not isinstance(value, str) or not file.pk:
        return None
    prefix = f"{schema}/content/{file.pk}/"
    if not value.startswith(prefix):
        return None
    filename = value[len(prefix) :]
    if "/" in filename or not is_safe_storage_filename(filename):
        return None
    return value


def trusted_thumbnail_key(file: LessonFile, *, schema: str) -> str | None:
    """Return a record-bound thumbnail key, including the deployed legacy form.

    The legacy path is accepted for existing rows only.  New thumbnails use a
    reserved derived-object directory, which cannot collide with an uploaded
    file named ``thumb.jpg``.
    """

    value = file.thumbnail_key
    if not isinstance(value, str) or not value or not file.pk:
        return None
    canonical = thumbnail_key(schema=schema, file_id=file.pk)
    legacy = f"{schema}/content/{file.pk}/{_LEGACY_THUMBNAIL}"
    if value == canonical:
        return value
    if value == legacy and value != file.s3_key:
        return value
    return None


def trusted_file_keys(file: LessonFile, *, schema: str) -> tuple[str, ...]:
    """Return only storage objects demonstrably owned by ``file``."""

    keys: list[str] = []
    pending = trusted_pending_key(file.s3_key, schema=schema)
    primary = trusted_primary_key(file, schema=schema)
    if pending:
        keys.append(pending)
    elif primary:
        keys.append(primary)
    thumb = trusted_thumbnail_key(file, schema=schema)
    if thumb and thumb not in keys:
        keys.append(thumb)
    return tuple(keys)


@dataclass(frozen=True)
class ParsedContentKey:
    kind: str
    file_id: int | None = None


def parse_content_key(value: object, *, schema: str) -> ParsedContentKey | None:
    """Validate a queued cleanup key against the domain's complete key grammar."""

    pending = trusted_pending_key(value, schema=schema)
    if pending:
        return ParsedContentKey(kind="pending")
    if not isinstance(value, str):
        return None
    prefix = f"{schema}/content/"
    if not value.startswith(prefix):
        return None
    parts = value[len(prefix) :].split("/")
    if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) > 0 and is_safe_storage_filename(parts[1]):
        return ParsedContentKey(kind="primary_or_legacy_thumbnail", file_id=int(parts[0]))
    if (
        len(parts) == 3
        and parts[0].isdigit()
        and int(parts[0]) > 0
        and "/".join(parts[1:]) == _DERIVED_THUMBNAIL
    ):
        return ParsedContentKey(kind="thumbnail", file_id=int(parts[0]))
    return None
