"""Shared in-memory S3 stub (D2-F-4).

Records puts/copies/deletes so the full signed-URL flow can be exercised without
a network dependency. Reused by D3-B payment receipt tests and D4-B report tests.
Wire it into a test via the `s3_stub` fixture (root conftest), which monkeypatches
every `apps.content.services` S3 helper onto an instance.
"""

from __future__ import annotations


class InMemoryS3:
    """A dict-backed stand-in shaped like the `infrastructure.storage.s3_client`
    helper surface (not the boto3 client itself)."""

    HELPER_NAMES = (
        "presign_upload",
        "presign_download",
        "upload_bytes",
        "head_object",
        "get_object_range",
        "download_bytes",
        "copy_object",
        "delete_object",
    )

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.copies: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    # --- client-side simulation -------------------------------------------
    def put(self, key: str, data: bytes) -> None:
        """Simulate the browser's direct PUT to the presigned URL."""
        self.objects[key] = data

    # --- helper-shaped methods (match s3_client signatures) ----------------
    def presign_upload(
        self,
        key,
        *,
        expires_in=600,
        content_type="application/octet-stream",
        size_bytes=None,
    ):
        return f"memory://put/{key}"

    def presign_download(
        self,
        key,
        *,
        expires_in=600,
        download_filename=None,
        response_content_type=None,
    ):
        return f"memory://get/{key}"

    def upload_bytes(self, key, data, *, content_type="application/octet-stream"):
        self.objects[key] = data
        self.puts.append(key)
        return key

    def head_object(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return {"ContentLength": len(self.objects[key])}

    def get_object_range(self, key, *, start=0, end=8191):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key][start : end + 1]

    def download_bytes(self, key, *, max_bytes=None):
        data = self.objects[key]
        if max_bytes is not None and len(data) > max_bytes:
            from infrastructure.storage.s3_client import StorageObjectTooLarge

            raise StorageObjectTooLarge("Storage object exceeds the permitted size")
        return data

    def copy_object(self, *, src_key, dest_key):
        self.objects[dest_key] = self.objects[src_key]
        self.copies.append((src_key, dest_key))
        return dest_key

    def delete_object(self, key):
        self.objects.pop(key, None)
        self.deletes.append(key)

    def install(self, monkeypatch) -> InMemoryS3:
        from apps.content import services

        for name in self.HELPER_NAMES:
            monkeypatch.setattr(services, name, getattr(self, name))
        return self
