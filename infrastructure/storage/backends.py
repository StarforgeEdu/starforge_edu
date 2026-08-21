"""Django storage backends for split internal and browser-facing S3 endpoints."""

from __future__ import annotations

from functools import cached_property
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import Storage
from django.core.files.utils import validate_file_name
from django.utils.encoding import filepath_to_uri
from storages.backends.s3 import S3Storage


class DualEndpointS3Storage(S3Storage):
    """Use the private endpoint for I/O and the public endpoint for URLs.

    MinIO is reached over the private Docker network for uploads, downloads, and
    ``collectstatic``.  URL generation is local SigV4 work, so a second storage
    instance can safely sign the browser-reachable endpoint without making a
    network request through the public reverse proxy.
    """

    def __init__(self, **options: Any) -> None:
        # Static assets must live on an origin distinct from user-controlled
        # media so the admin CSP can authorize scripts without authorizing an
        # uploaded object as executable code.
        self._explicit_public_endpoint = str(options.pop("public_endpoint_url", "")).strip()
        self._public_storage_options = options.copy()
        super().__init__(**options)

    @cached_property
    def _public_url_storage(self) -> S3Storage:
        public_endpoint = (
            self._explicit_public_endpoint or getattr(settings, "AWS_S3_PUBLIC_ENDPOINT_URL", "").strip()
        )
        if not public_endpoint:
            raise ImproperlyConfigured(
                "public_endpoint_url or AWS_S3_PUBLIC_ENDPOINT_URL is required to generate "
                "browser-facing storage URLs."
            )
        options = self._public_storage_options.copy()
        options["endpoint_url"] = public_endpoint
        return S3Storage(**options)

    def url(
        self,
        name: str,
        parameters: dict[str, Any] | None = None,
        expire: int | None = None,
        http_method: str | None = None,
    ) -> str:
        return self._public_url_storage.url(
            name,
            parameters=parameters,
            expire=expire,
            http_method=http_method,
        )


class DisabledObjectStorage(Storage):
    """Fail-closed storage for a process that must have no object authority."""

    disabled_message = "Object storage is disabled in this isolated process."

    @classmethod
    def _io_disabled(cls) -> ImproperlyConfigured:
        return ImproperlyConfigured(cls.disabled_message)

    def _open(self, name: str, mode: str = "rb"):
        raise self._io_disabled()

    def _save(self, name: str, content) -> str:
        raise self._io_disabled()

    def delete(self, name: str) -> None:
        raise self._io_disabled()

    def exists(self, name: str) -> bool:
        raise self._io_disabled()

    def listdir(self, path: str) -> tuple[list[str], list[str]]:
        raise self._io_disabled()

    def size(self, name: str) -> int:
        raise self._io_disabled()

    def url(self, name: str | None) -> str:
        raise self._io_disabled()


class PublicStaticFilesStorage(DisabledObjectStorage):
    """URL-only static storage used by every long-running production process.

    The static origin is CSP-trusted and therefore materially more sensitive
    than user-controlled media. Runtime processes have no static credential
    and this backend has no S3 client or credential-provider chain at all.
    Only the one-shot ``collectstatic`` service selects the writable S3 backend.
    """

    disabled_message = (
        "Static object I/O is disabled in runtime processes; use the isolated collectstatic service."
    )

    def __init__(self, *, bucket_name: str, public_endpoint_url: str) -> None:
        self.bucket_name = str(bucket_name).strip()
        self.public_endpoint_url = str(public_endpoint_url).strip().rstrip("/")
        if not self.bucket_name or not self.public_endpoint_url:
            raise ImproperlyConfigured("The public static bucket and endpoint must be configured explicitly.")

    def url(self, name: str | None) -> str:
        if name is None:
            raise ValueError("Static object name must not be empty")
        normalized = str(name).replace("\\", "/").lstrip("/")
        validate_file_name(normalized, allow_relative_path=True)
        if not normalized:
            raise ValueError("Static object name must not be empty")
        return f"{self.public_endpoint_url}/{filepath_to_uri(self.bucket_name)}/{filepath_to_uri(normalized)}"
