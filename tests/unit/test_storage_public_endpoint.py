from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.test import override_settings

from infrastructure.storage import s3_client
from infrastructure.storage.backends import DualEndpointS3Storage, PublicStaticFilesStorage

INTERNAL_ENDPOINT = "http://minio:9000"
PUBLIC_ENDPOINT = "https://storage.starforge.78.111.91.113.nip.io"
STATIC_PUBLIC_ENDPOINT = "https://static-storage.starforge.78.111.91.113.nip.io"
STORAGE_OPTIONS = {
    "bucket_name": "starforge-media",
    "endpoint_url": INTERNAL_ENDPOINT,
    "access_key": "test-access-key",
    "secret_key": "test-secret-key",
    "region_name": "us-east-1",
    "addressing_style": "path",
    "signature_version": "s3v4",
    "file_overwrite": False,
}
S3_STORAGES = {
    "default": {
        "BACKEND": "infrastructure.storage.backends.DualEndpointS3Storage",
        "OPTIONS": STORAGE_OPTIONS,
    }
}


@pytest.fixture(autouse=True)
def _clear_s3_client_caches():
    s3_client.get_s3_client.cache_clear()
    s3_client.get_s3_presign_client.cache_clear()
    yield
    s3_client.get_s3_client.cache_clear()
    s3_client.get_s3_presign_client.cache_clear()


def _assert_public_url(
    url: str,
    *,
    bucket: str,
    key: str,
    hostname: str = "storage.starforge.78.111.91.113.nip.io",
) -> None:
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.hostname == hostname
    expected_path = f"/{bucket}/{key}" if key else f"/{bucket}"
    assert parsed.path == expected_path
    assert "minio" not in url


@override_settings(STORAGES=S3_STORAGES, AWS_S3_PUBLIC_ENDPOINT_URL=PUBLIC_ENDPOINT)
def test_presigns_use_public_endpoint_while_object_io_stays_internal():
    key = "tenant_a/content/example.pdf"

    put_url = s3_client.presign_upload(key, content_type="application/pdf")
    post = s3_client.presign_post_upload(
        key,
        size_bytes=123,
        content_type="application/pdf",
    )
    get_url = s3_client.presign_download(key)

    _assert_public_url(put_url, bucket="starforge-media", key=key)
    _assert_public_url(post["url"], bucket="starforge-media", key="")
    _assert_public_url(get_url, bucket="starforge-media", key=key)
    assert "X-Amz-Signature" in parse_qs(urlsplit(put_url).query)
    assert "X-Amz-Signature" in parse_qs(urlsplit(get_url).query)
    assert post["fields"]["key"] == key
    assert s3_client.get_s3_client().meta.endpoint_url == INTERNAL_ENDPOINT
    assert s3_client.get_s3_presign_client().meta.endpoint_url == PUBLIC_ENDPOINT


@override_settings(STORAGES=S3_STORAGES, AWS_S3_PUBLIC_ENDPOINT_URL=PUBLIC_ENDPOINT)
def test_download_presign_forces_attachment_disposition_without_header_injection():
    url = s3_client.presign_download(
        "tenant_a/messaging/messages/1/2/report.pdf",
        download_filename='report "final".pdf',
        response_content_type="application/pdf",
    )

    query = parse_qs(urlsplit(url).query)
    assert query["response-content-disposition"] == ["attachment; filename*=UTF-8''report%20%22final%22.pdf"]
    assert query["response-content-type"] == ["application/pdf"]


@override_settings(AWS_S3_PUBLIC_ENDPOINT_URL=PUBLIC_ENDPOINT)
def test_storage_backend_uses_internal_io_and_public_signed_media_urls():
    storage = DualEndpointS3Storage(**STORAGE_OPTIONS)

    assert storage.connection.meta.client.meta.endpoint_url == INTERNAL_ENDPOINT
    url = storage.url("students/photos/avatar.jpg")
    _assert_public_url(
        url,
        bucket="starforge-media",
        key="students/photos/avatar.jpg",
    )
    assert "X-Amz-Signature" in parse_qs(urlsplit(url).query)


@override_settings(AWS_S3_PUBLIC_ENDPOINT_URL=PUBLIC_ENDPOINT)
def test_static_storage_url_is_public_and_unsigned_but_writes_stay_internal():
    options = {
        **STORAGE_OPTIONS,
        "bucket_name": "starforge-static",
        "querystring_auth": False,
        "public_endpoint_url": STATIC_PUBLIC_ENDPOINT,
    }
    storage = DualEndpointS3Storage(**options)

    assert storage.connection.meta.client.meta.endpoint_url == INTERNAL_ENDPOINT
    url = storage.url("admin/css/base.css")
    _assert_public_url(
        url,
        bucket="starforge-static",
        key="admin/css/base.css",
        hostname="static-storage.starforge.78.111.91.113.nip.io",
    )
    assert urlsplit(url).query == ""


def test_runtime_static_storage_generates_public_urls_without_an_s3_client(monkeypatch):
    monkeypatch.setattr(
        "boto3.client",
        lambda *args, **kwargs: pytest.fail("URL-only static storage must not create an S3 client"),
    )
    storage = PublicStaticFilesStorage(
        bucket_name="starforge-static",
        public_endpoint_url=STATIC_PUBLIC_ENDPOINT,
    )

    url = storage.url("admin/css/base file.css")

    _assert_public_url(
        url,
        bucket="starforge-static",
        key="admin/css/base%20file.css",
        hostname="static-storage.starforge.78.111.91.113.nip.io",
    )
    assert urlsplit(url).query == ""


@pytest.mark.parametrize(
    "operation",
    [
        lambda storage: storage.open("admin/css/base.css"),
        lambda storage: storage.save("admin/css/base.css", ContentFile(b"body")),
        lambda storage: storage.delete("admin/css/base.css"),
        lambda storage: storage.exists("admin/css/base.css"),
        lambda storage: storage.listdir("admin/css"),
        lambda storage: storage.size("admin/css/base.css"),
    ],
)
def test_runtime_static_storage_rejects_all_object_io(operation):
    storage = PublicStaticFilesStorage(
        bucket_name="starforge-static",
        public_endpoint_url=STATIC_PUBLIC_ENDPOINT,
    )

    with pytest.raises(ImproperlyConfigured, match="isolated collectstatic"):
        operation(storage)


@override_settings(STORAGES=S3_STORAGES, AWS_S3_PUBLIC_ENDPOINT_URL="")
def test_browser_url_generation_fails_closed_without_public_endpoint():
    with pytest.raises(ImproperlyConfigured, match="AWS_S3_PUBLIC_ENDPOINT_URL"):
        s3_client.presign_download("tenant_a/report.pdf")

    storage = DualEndpointS3Storage(**STORAGE_OPTIONS)
    with pytest.raises(ImproperlyConfigured, match="AWS_S3_PUBLIC_ENDPOINT_URL"):
        storage.url("students/photos/avatar.jpg")
