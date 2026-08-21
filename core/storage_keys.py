"""Small, domain-neutral building blocks for private object-store keys.

Keys are capabilities once a download URL is signed.  Treat every key read
from the database or a request as untrusted and require each domain to parse
its complete grammar before storage I/O.  This module only owns safe key
segments; record binding remains in each domain.
"""

from __future__ import annotations

import re
import unicodedata

_LOWER_HEX_UUID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_FILENAME_CHARS = 255
_MAX_FILENAME_BYTES = 255
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


def normalized_storage_filename(value: object) -> str | None:
    """Return a canonical, single-segment Unicode filename or ``None``.

    Unicode letters are intentionally supported, but path separators, control
    and formatting characters, ambiguous dot segments, non-normalized text,
    and values too large for predictable object-key/header handling are not.
    """

    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFC", value)
    if value != normalized or not value or len(value) > _MAX_FILENAME_CHARS:
        return None
    if value != value.strip() or value in {".", ".."} or "/" in value or "\\" in value:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    try:
        if len(value.encode("utf-8")) > _MAX_FILENAME_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    return value


def is_lower_hex_upload_id(value: object) -> bool:
    return isinstance(value, str) and bool(_LOWER_HEX_UUID.fullmatch(value))


def positive_decimal_id(value: object) -> int | None:
    """Parse a canonical positive base-10 identifier without aliases."""

    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    if value.startswith("0") or len(value) > 19:
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= _MAX_SIGNED_BIGINT else None
