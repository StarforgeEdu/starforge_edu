from __future__ import annotations

import pytest

from infrastructure.storage import s3_client


class _Body:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False
        self.requested: int | None = None
        self.offset = 0

    def read(self, amount: int) -> bytes:
        self.requested = amount
        chunk = self.payload[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, body: _Body) -> None:
        self.body = body

    def get_object(self, **_kwargs):
        return {"Body": self.body}


def test_get_object_range_bounds_and_closes_an_untrusted_response(monkeypatch):
    body = _Body(b"x" * 10_000)
    monkeypatch.setattr(s3_client, "get_s3_client", lambda: _Client(body))
    monkeypatch.setattr(s3_client, "_storage_options", lambda: {"bucket_name": "private"})

    with pytest.raises(s3_client.StorageObjectTooLarge):
        s3_client.get_object_range("tenant/content/object", start=0, end=8191)

    assert body.requested == 8193
    assert body.closed is True


@pytest.mark.parametrize(
    ("start", "end"),
    [(-1, 1), (2, 1), (False, 1), (0, 65_536)],
)
def test_get_object_range_rejects_invalid_or_excessive_ranges(start, end):
    with pytest.raises(ValueError, match=r"object byte range|Object byte range"):
        s3_client.get_object_range("unused", start=start, end=end)


def test_download_to_path_streams_and_enforces_exact_size(monkeypatch, tmp_path):
    payload = b"authoritative-pdf-bytes"
    body = _Body(payload)
    client = _Client(body)
    client.get_object = lambda **_kwargs: {"Body": body, "ContentLength": len(payload)}
    monkeypatch.setattr(s3_client, "get_s3_client", lambda: client)
    monkeypatch.setattr(s3_client, "_storage_options", lambda: {"bucket_name": "private"})
    destination = tmp_path / "document.pdf"

    assert s3_client.download_to_path(
        "tenant/document",
        destination,
        max_bytes=1024,
        expected_size_bytes=len(payload),
    ) == len(payload)
    assert destination.read_bytes() == payload
    assert body.closed is True


def test_download_to_path_rejects_metadata_size_mismatch(monkeypatch, tmp_path):
    body = _Body(b"small")
    client = _Client(body)
    client.get_object = lambda **_kwargs: {"Body": body, "ContentLength": 5}
    monkeypatch.setattr(s3_client, "get_s3_client", lambda: client)
    monkeypatch.setattr(s3_client, "_storage_options", lambda: {"bucket_name": "private"})

    with pytest.raises(s3_client.StorageObjectSizeMismatch):
        s3_client.download_to_path(
            "tenant/document",
            tmp_path / "document.pdf",
            max_bytes=1024,
            expected_size_bytes=6,
        )
    assert body.closed is True
