from __future__ import annotations

import os
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core.security_config import validate_exact_https_origins


def _production_import_environment(*, soliq_allowed_hosts: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.production",
            "SECRET_KEY": "test-only-production-settings-key-0000000000000000000000000000",
            "ALLOWED_HOSTS": "example.invalid",
            "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
            "NUM_PROXIES": "1",
            "DATABASE_URL": "postgres://ci:ci@127.0.0.1:5432/ci",
            "REDIS_URL": "redis://127.0.0.1:6379/0",
            "AWS_STORAGE_BUCKET_NAME": "ci-media",
            "AWS_STATIC_BUCKET_NAME": "ci-static",
            "AWS_S3_ENDPOINT_URL": "https://storage.example.invalid",
            "AWS_S3_PUBLIC_ENDPOINT_URL": "https://media.example.invalid",
            "AWS_STATIC_PUBLIC_ENDPOINT_URL": "https://static.example.invalid",
            "AWS_S3_ACCESS_KEY_ID": "ci-media-runtime",
            "AWS_S3_SECRET_ACCESS_KEY": "ci-media-runtime-secret-key-0000000000000000",
            "AWS_S3_REGION_NAME": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "STATIC_STORAGE_WRITE_ENABLED": "False",
            "CORS_ALLOWED_ORIGINS": "",
            "CSRF_TRUSTED_ORIGINS": "",
            "WEBSOCKET_ALLOWED_ORIGINS": "",
            "CLICK_CHECKOUT_URL": "https://my.click.uz/services/pay",
            "CLICK_CHECKOUT_ALLOWED_HOSTS": "my.click.uz",
            "PAYME_CHECKOUT_URL": "https://checkout.paycom.uz",
            "PAYME_CHECKOUT_ALLOWED_HOSTS": "checkout.paycom.uz",
            "DOMAIN_VERIFICATION_DNS_URL": "https://cloudflare-dns.com/dns-query",
            "DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS": "cloudflare-dns.com",
            "SMS_ENABLED": "False",
            "AI_ENABLED": "False",
            "EMAIL_ENABLED": "False",
            "PUSH_NOTIFICATIONS_ENABLED": "False",
            "FISCALIZATION_ENABLED": "True",
            "SOLIQ_API_URL": "https://soliq.example.invalid",
            "SOLIQ_API_ALLOWED_HOSTS": soliq_allowed_hosts,
            "SOLIQ_API_TOKEN": "ci-only",
        }
    )
    environment.pop("AWS_STATIC_ACCESS_KEY_ID", None)
    environment.pop("AWS_STATIC_SECRET_ACCESS_KEY", None)
    return environment


def _import_production_settings(*, soliq_allowed_hosts: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=_production_import_environment(soliq_allowed_hosts=soliq_allowed_hosts),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_cors_wraps_pre_tenant_rate_limit_responses():
    middleware = list(settings.MIDDLEWARE)
    assert middleware.index("corsheaders.middleware.CorsMiddleware") < middleware.index(
        "core.middleware.ApiRateLimitMiddleware"
    )


def test_staging_forces_every_external_provider_to_mock():
    source = (settings.BASE_DIR / "config" / "settings" / "staging.py").read_text(encoding="utf-8")
    for name in (
        "ESKIZ_USE_MOCK",
        "ANTHROPIC_USE_MOCK",
        "CLICK_USE_MOCK",
        "PAYME_USE_MOCK",
        "UZUM_USE_MOCK",
        "FINANCE_FX_USE_MOCK",
        "SOLIQ_USE_MOCK",
        "FCM_USE_MOCK",
        "PLATFORM_PAYMENTS_USE_MOCK",
    ):
        assert f"{name} = True" in source


def test_production_forces_every_financial_provider_and_fx_source_real():
    source = (settings.BASE_DIR / "config" / "settings" / "production.py").read_text(encoding="utf-8")
    for name in (
        "CLICK_USE_MOCK",
        "PAYME_USE_MOCK",
        "UZUM_USE_MOCK",
        "FINANCE_FX_USE_MOCK",
        "SOLIQ_USE_MOCK",
        "PLATFORM_PAYMENTS_USE_MOCK",
    ):
        assert f"{name} = False" in source


def test_deployment_settings_force_test_only_legacy_auth_adapters_off():
    for settings_name in ("production.py", "staging.py"):
        source = (settings.BASE_DIR / "config" / "settings" / settings_name).read_text(encoding="utf-8")
        for flag in (
            "ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS",
            "ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS",
        ):
            assert f"{flag} = False" in source


def test_production_soliq_url_without_allowlist_fails_closed():
    result = _import_production_settings(soliq_allowed_hosts="")

    assert result.returncode != 0
    assert "SOLIQ_API_URL requires a non-empty exact hostname allowlist" in result.stderr


def test_production_soliq_matching_allowlist_boots():
    result = _import_production_settings(soliq_allowed_hosts="soliq.example.invalid")

    assert result.returncode == 0, result.stderr


def test_production_rejects_invalid_branch_agent_rate_limit():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment["API_RATELIMIT_AGENT"] = "0/min"

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "API_RATELIMIT_AGENT" in result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_AVAILABILITY_CACHE_TIMEOUT_SECONDS", "0"),
        ("APP_AVAILABILITY_CACHE_TIMEOUT_SECONDS", "301"),
        ("PRINT_AGENT_LEASE_SECONDS", "59"),
        ("PRINT_AGENT_LEASE_SECONDS", "3601"),
        ("PRINT_STALE_LEASE_SWEEP_BATCH_SIZE", "0"),
        ("PRINT_STALE_LEASE_SWEEP_BATCH_SIZE", "1001"),
    ],
)
def test_production_rejects_unsafe_cache_and_lease_configuration(name, value):
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment[name] = value

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert name in result.stderr


@pytest.mark.parametrize(
    "field_key",
    [
        "not-a-fernet-key",
        "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        "c3RhcmZvcmdlLWRldi1maWVsZGVuYy1rZXktMzJieXQ=",
    ],
)
def test_production_rejects_invalid_or_published_field_encryption_keys(field_key):
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment["FIELD_ENCRYPTION_KEY"] = field_key

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "FIELD_ENCRYPTION_KEY" in result.stderr


@pytest.mark.parametrize(
    "secret_key",
    [
        "a" * 80,
        "valid-looking-but-contains-a-newline\n" + "x" * 50,
    ],
)
def test_production_rejects_obviously_weak_or_ambiguous_secret_keys(secret_key):
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment["SECRET_KEY"] = secret_key

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "SECRET_KEY" in result.stderr


def test_exact_https_origin_allowlist_accepts_only_origins():
    assert validate_exact_https_origins(
        "CORS_ALLOWED_ORIGINS",
        ["https://console.example.test", "https://admin.example.test:8443"],
    ) == ("https://console.example.test", "https://admin.example.test:8443")


@pytest.mark.parametrize(
    "origins",
    [
        "https://console.example.test",
        ["http://console.example.test"],
        ["https://user:secret@console.example.test"],
        ["https://console.example.test/path"],
        ["https://console.example.test?next=evil"],
        ["https://*.example.test"],
        ["https://console.example.test\\@evil.test"],
        ["https://console.example.test\n.evil.test"],
        ["https://console.example.test:0"],
        ["https://console.example.test:99999"],
        ["https://console.example.test", "HTTPS://CONSOLE.EXAMPLE.TEST"],
    ],
)
def test_exact_https_origin_allowlist_rejects_ambiguous_or_insecure_values(origins):
    with pytest.raises(ImproperlyConfigured):
        validate_exact_https_origins("CORS_ALLOWED_ORIGINS", origins)


def test_production_rejects_non_default_websocket_port():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment["WEBSOCKET_ALLOWED_ORIGINS"] = "https://console.example.invalid:8443"

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "WEBSOCKET_ALLOWED_ORIGINS must use the default HTTPS port" in result.stderr


def test_production_rejects_ambiguous_storage_origin():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment["AWS_S3_PUBLIC_ENDPOINT_URL"] = "https://media.example.invalid:0"

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "AWS_S3_PUBLIC_ENDPOINT_URL" in result.stderr


def test_production_runtime_uses_url_only_static_backend_without_static_credentials():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config.settings.production as s; "
            "print(s.STORAGES['staticfiles']['BACKEND']); "
            "print(sorted(s.STORAGES['staticfiles']['OPTIONS']))",
        ],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PublicStaticFilesStorage" in result.stdout
    assert "'access_key'" not in result.stdout
    assert "'secret_key'" not in result.stdout
    assert "'endpoint_url'" not in result.stdout


def test_production_rejects_metadata_credential_fallback():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment["AWS_EC2_METADATA_DISABLED"] = "false"

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "AWS_EC2_METADATA_DISABLED" in result.stderr


def test_production_rejects_static_writer_credentials_in_runtime():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment.update(
        {
            "AWS_STATIC_ACCESS_KEY_ID": "ci-static-writer",
            "AWS_STATIC_SECRET_ACCESS_KEY": "ci-static-writer-secret-key-000000000000000",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "must not be present" in result.stderr


def test_collectstatic_requires_and_uses_a_distinct_explicit_credential():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment.update(
        {
            "STATIC_STORAGE_WRITE_ENABLED": "True",
            "AWS_S3_ACCESS_KEY_ID": "",
            "AWS_S3_SECRET_ACCESS_KEY": "",
            "AWS_STATIC_ACCESS_KEY_ID": "ci-static-writer",
            "AWS_STATIC_SECRET_ACCESS_KEY": "ci-static-writer-secret-key-000000000000000",
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config.settings.production as s; "
            "o=s.STORAGES['staticfiles']['OPTIONS']; "
            "print(s.STORAGES['staticfiles']['BACKEND']); "
            "print(o['access_key']); print(len(o['secret_key'])); "
            "print(s.STORAGES['default']['BACKEND'])",
        ],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "DualEndpointS3Storage" in result.stdout
    assert "ci-static-writer" in result.stdout
    assert "DisabledObjectStorage" in result.stdout


def test_collectstatic_rejects_any_media_credential():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment.update(
        {
            "STATIC_STORAGE_WRITE_ENABLED": "True",
            "AWS_STATIC_ACCESS_KEY_ID": "ci-static-writer",
            "AWS_STATIC_SECRET_ACCESS_KEY": "ci-static-writer-secret-key-000000000000000",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert "must not receive media-runtime credentials" in result.stderr


@pytest.mark.parametrize(
    ("overrides", "expected_error"),
    [
        ({"EMAIL_USE_TLS": "False"}, "EMAIL_USE_TLS must be enabled"),
        ({"EMAIL_PORT": "0"}, "EMAIL_PORT must be between 1 and 65535"),
        ({"EMAIL_PORT": "65536"}, "EMAIL_PORT must be between 1 and 65535"),
        (
            {"EMAIL_HOST_USER": "mailer", "EMAIL_HOST_PASSWORD": ""},
            "EMAIL_HOST_USER and EMAIL_HOST_PASSWORD must be supplied together",
        ),
    ],
)
def test_production_email_delivery_fails_closed_on_insecure_configuration(overrides, expected_error):
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment.update(
        {
            "EMAIL_ENABLED": "True",
            "EMAIL_HOST": "smtp.example.invalid",
            "EMAIL_PORT": "587",
            "EMAIL_USE_TLS": "True",
            **overrides,
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_production_email_delivery_accepts_starttls_configuration():
    environment = _production_import_environment(soliq_allowed_hosts="soliq.example.invalid")
    environment.update(
        {
            "EMAIL_ENABLED": "True",
            "EMAIL_HOST": "smtp.example.invalid",
            "EMAIL_PORT": "587",
            "EMAIL_HOST_USER": "mailer",
            "EMAIL_HOST_PASSWORD": "test-only",
            "EMAIL_USE_TLS": "True",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import config.settings.production"],
        cwd=settings.BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
