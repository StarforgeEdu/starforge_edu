"""Bounded verification and promotion for user-uploaded attachments."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from botocore.exceptions import ClientError

from infrastructure.storage.s3_client import copy_object, delete_object, get_object_range, head_object

_EXTENSION_MIME: dict[str, frozenset[str]] = {
    "pdf": frozenset({"application/pdf"}),
    "mp4": frozenset({"video/mp4"}),
    "pptx": frozenset({"application/vnd.openxmlformats-officedocument.presentationml.presentation"}),
    "docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    "mp3": frozenset({"audio/mpeg"}),
    # Flutter's native AAC recorder writes an ISO-BMFF M4A container and sends
    # the standards-based upload type audio/mp4. libmagic identifies the same
    # reviewed container signature as audio/x-m4a on common production images,
    # so declared and sniffed MIME contracts are deliberately separated below.
    "m4a": frozenset({"audio/mp4"}),
    "webm": frozenset({"audio/webm"}),
    "jpg": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "png": frozenset({"image/png"}),
    "webp": frozenset({"image/webp"}),
}

_EXTENSION_SNIFFED_MIME: dict[str, frozenset[str]] = {
    "m4a": frozenset({"audio/x-m4a", "audio/mp4"}),
    # Chromium records Opus voice notes in a WebM container. libmagic may
    # describe an audio-only stream as either audio/webm or video/webm.
    "webm": frozenset({"audio/webm", "video/webm"}),
}


class AttachmentObjectError(ValueError):
    """A staged/final object did not satisfy its immutable upload contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class VerifiedAttachment:
    size_bytes: int
    content_type: str
    sniffed_type: str


def _missing_object(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code", "")) in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


def _sniff_mime(payload: bytes) -> str:
    import magic

    return str(magic.from_buffer(payload, mime=True)).strip().lower()


def allowed_attachment_mime_types(filename: str) -> frozenset[str]:
    """Return the exact MIME allowlist for a supported attachment extension."""

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXTENSION_MIME.get(extension, frozenset())


def attachment_content_matches(*, filename: str, declared: str, sniffed: str) -> bool:
    """Match one extension against its reviewed declared and sniffed MIME sets."""

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = allowed_attachment_mime_types(filename)
    expected_sniffed = _EXTENSION_SNIFFED_MIME.get(extension, expected)
    # Fail closed for an organization-configured extension that has no reviewed
    # signature mapping. Generic ZIP is intentionally insufficient evidence for
    # DOCX/PPTX because any archive can be relabelled with those extensions.
    return bool(expected) and declared in expected and sniffed in expected_sniffed


def verify_attachment_object(
    key: str,
    *,
    filename: str,
    expected_size_bytes: int,
    expected_content_type: str,
) -> VerifiedAttachment:
    """Verify metadata and a bounded content signature for one exact key."""

    try:
        metadata = head_object(key)
    except FileNotFoundError as exc:
        raise AttachmentObjectError("missing") from exc
    except ClientError as exc:
        if _missing_object(exc):
            raise AttachmentObjectError("missing") from exc
        raise

    try:
        actual_size = int(metadata.get("ContentLength", -1))
    except (TypeError, ValueError):
        actual_size = -1
    if actual_size != expected_size_bytes or actual_size < 1:
        raise AttachmentObjectError("size")

    actual_type = str(metadata.get("ContentType", "")).partition(";")[0].strip().lower()
    declared = expected_content_type.partition(";")[0].strip().lower()
    if not declared or actual_type != declared:
        raise AttachmentObjectError("content_type")

    sniffed = _sniff_mime(get_object_range(key, start=0, end=8191))
    if not attachment_content_matches(filename=filename, declared=declared, sniffed=sniffed):
        raise AttachmentObjectError("content")
    return VerifiedAttachment(
        size_bytes=actual_size,
        content_type=actual_type,
        sniffed_type=sniffed,
    )


def promote_attachment_object(
    *,
    source_key: str,
    destination_key: str,
    filename: str,
    expected_size_bytes: int,
    expected_content_type: str,
) -> VerifiedAttachment:
    """Verify a staged upload, copy it to a non-uploadable key, and reverify it."""

    verify_attachment_object(
        source_key,
        filename=filename,
        expected_size_bytes=expected_size_bytes,
        expected_content_type=expected_content_type,
    )
    try:
        # Copy can succeed server-side while the client times out before seeing
        # the response. Keep it inside the compensation boundary so that an
        # ambiguous copy never leaves an unreferenced durable object behind.
        copy_object(src_key=source_key, dest_key=destination_key)
        return verify_attachment_object(
            destination_key,
            filename=filename,
            expected_size_bytes=expected_size_bytes,
            expected_content_type=expected_content_type,
        )
    except Exception:
        with suppress(Exception):
            delete_object(destination_key)
        # Preserve the verification/storage exception.  The deterministic
        # final key is safe to overwrite on retry and is covered by the
        # record-lifecycle cleanup task once the row becomes durable.
        raise
