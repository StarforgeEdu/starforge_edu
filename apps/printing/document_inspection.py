"""Bounded, server-authoritative page counting for printable user documents."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from infrastructure.storage.s3_client import (
    StorageObjectSizeMismatch,
    StorageObjectTooLarge,
    download_to_path,
)

MAX_PRINT_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_PRINT_DOCUMENT_PAGES = 10_000
_PDFINFO_TIMEOUT_SECONDS = 8
_PDFINFO_OUTPUT_BYTES = 128 * 1024
_PDFINFO_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_PAGES_LINE = re.compile(rb"(?m)^Pages:\s*([1-9][0-9]*)\s*$")
_ENCRYPTED_LINE = re.compile(rb"(?m)^Encrypted:\s*yes(?:\s|$)", re.IGNORECASE)


class PrintDocumentInspectionError(ValueError):
    """The object cannot be accepted as a bounded printable document."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PrintDocumentInspectionUnavailable(RuntimeError):
    """Storage or the sandboxed PDF inspector is temporarily unavailable."""


def _is_missing_object(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code", "")) in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


def _read_bounded(handle) -> bytes:
    handle.seek(0)
    payload = handle.read(_PDFINFO_OUTPUT_BYTES + 1)
    if len(payload) > _PDFINFO_OUTPUT_BYTES:
        raise PrintDocumentInspectionError("pdf_output")
    return payload


def _pdf_page_count(path: Path) -> int:
    pdfinfo = shutil.which("pdfinfo", path="/usr/bin:/bin")
    prlimit = shutil.which("prlimit", path="/usr/bin:/bin")
    if pdfinfo is None or prlimit is None:
        raise PrintDocumentInspectionUnavailable("PDF inspection tools are unavailable")

    # prlimit applies hard OS resource ceilings without unsafe preexec_fn use in
    # a multi-threaded web process. Output goes to bounded regular files; an
    # adversarial metadata field cannot fill a pipe or allocate unbounded RAM.
    command = [
        prlimit,
        f"--as={_PDFINFO_ADDRESS_SPACE_BYTES}",
        "--cpu=5",
        f"--fsize={_PDFINFO_OUTPUT_BYTES}",
        "--nofile=64",
        "--core=0",
        "--",
        pdfinfo,
        str(path),
    ]
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=path.parent,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                check=False,
                timeout=_PDFINFO_TIMEOUT_SECONDS,
                start_new_session=True,
            )
            output = _read_bounded(stdout)
            _read_bounded(stderr)
    except subprocess.TimeoutExpired as exc:
        raise PrintDocumentInspectionError("pdf_complexity") from exc
    except OSError as exc:
        raise PrintDocumentInspectionUnavailable("PDF inspection could not start") from exc

    # Poppler may emit benign syntax warnings for a document it can still count.
    # A non-zero exit is always rejected; bounded stderr alone is not.
    if completed.returncode != 0:
        raise PrintDocumentInspectionError("pdf_invalid")
    if _ENCRYPTED_LINE.search(output):
        raise PrintDocumentInspectionError("pdf_encrypted")
    matches = _PAGES_LINE.findall(output)
    if len(matches) != 1:
        raise PrintDocumentInspectionError("pdf_invalid")
    pages = int(matches[0])
    if not 1 <= pages <= MAX_PRINT_DOCUMENT_PAGES:
        raise PrintDocumentInspectionError("page_limit")
    return pages


def authoritative_page_count(
    *,
    key: str,
    content_type: str,
    expected_size_bytes: int,
) -> int:
    """Return a trusted physical page count without trusting mobile input.

    Images are one physical page. PDFs are streamed to a private temporary file
    with an exact byte contract, then parsed by Poppler under CPU, memory, file,
    descriptor, wall-time, and output ceilings.
    """

    normalized_type = content_type.partition(";")[0].strip().lower()
    if (
        isinstance(expected_size_bytes, bool)
        or not isinstance(expected_size_bytes, int)
        or not 1 <= expected_size_bytes <= MAX_PRINT_DOCUMENT_BYTES
    ):
        raise PrintDocumentInspectionError("size")
    if normalized_type in _IMAGE_TYPES:
        return 1
    if normalized_type != "application/pdf":
        raise PrintDocumentInspectionError("type")

    try:
        with tempfile.TemporaryDirectory(prefix="starforge-print-") as directory:
            path = Path(directory) / "document.pdf"
            download_to_path(
                key,
                path,
                max_bytes=MAX_PRINT_DOCUMENT_BYTES,
                expected_size_bytes=expected_size_bytes,
            )
            return _pdf_page_count(path)
    except (StorageObjectTooLarge, StorageObjectSizeMismatch) as exc:
        raise PrintDocumentInspectionError("size") from exc
    except FileNotFoundError as exc:
        raise PrintDocumentInspectionError("missing") from exc
    except ClientError as exc:
        if _is_missing_object(exc):
            raise PrintDocumentInspectionError("missing") from exc
        raise PrintDocumentInspectionUnavailable("Document storage is unavailable") from exc
    except BotoCoreError as exc:
        raise PrintDocumentInspectionUnavailable("Document storage is unavailable") from exc
    except OSError as exc:
        raise PrintDocumentInspectionUnavailable("Document inspection storage is unavailable") from exc
