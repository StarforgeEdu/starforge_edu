"""Production settings."""

import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

from core.rate_config import RateConfigurationError, parse_rate
from core.security_config import validate_exact_https_origins

from .base import *  # noqa: F403
from .base import (
    AI_ENABLED,
    APP_AVAILABILITY_CACHE_TIMEOUT_SECONDS,
    CLICK_CHECKOUT_ALLOWED_HOSTS,
    CLICK_CHECKOUT_URL,
    CORS_ALLOWED_ORIGINS,
    CSRF_TRUSTED_ORIGINS,
    DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS,
    DOMAIN_VERIFICATION_DNS_URL,
    EMAIL_ENABLED,
    ESKIZ_API_ALLOWED_HOSTS,
    ESKIZ_API_URL,
    FIELD_ENCRYPTION_KEY,
    FISCALIZATION_ENABLED,
    PAYME_CHECKOUT_ALLOWED_HOSTS,
    PAYME_CHECKOUT_URL,
    PRINT_AGENT_LEASE_SECONDS,
    PRINT_STALE_LEASE_SWEEP_BATCH_SIZE,
    PUSH_NOTIFICATIONS_ENABLED,
    SESSION_IDLE_TIMEOUT_MINUTES,
    SESSION_TTL_DAYS,
    SMS_ENABLED,
    SOLIQ_API_ALLOWED_HOSTS,
    SOLIQ_API_URL,
    WEBSOCKET_ALLOWED_ORIGINS,
    WEBSOCKET_CONNECTION_LEASE_SECONDS,
    WEBSOCKET_HANDSHAKE_RATE_LIMIT,
    WEBSOCKET_MAX_CONNECTIONS_PER_SESSION,
    WEBSOCKET_USER_CONNECT_RATE_LIMIT,
    env,
)

DEBUG = False

# Only platform-owned suffixes may skip customer DNS verification. This is
# deliberately empty unless the operator names the deployment's own base domain.
DOMAIN_VERIFICATION_TRUSTED_SUFFIXES = env.list(
    "DOMAIN_VERIFICATION_TRUSTED_SUFFIXES",
    default=[],
)

# Fail fast on insecure defaults — base.py ships dev-friendly fallbacks
# (`dev-only-CHANGE-ME`, ALLOWED_HOSTS=["*"]) that must NEVER reach production:
# the default SECRET_KEY would let anyone forge signed data/sessions, and a wildcard
# host disables Host-header validation.
SECRET_KEY = env("SECRET_KEY")
# Reject the dev default AND obviously low-entropy/control-containing values
# because Django signs security-sensitive values with it
# (``get_random_secret_key`` produces 50 varied printable characters).
if (
    not SECRET_KEY
    or SECRET_KEY == "dev-only-CHANGE-ME"
    or len(SECRET_KEY) < 50
    or len(set(SECRET_KEY)) < 5
    or any(ord(character) < 33 or ord(character) == 127 for character in SECRET_KEY)
):
    raise ImproperlyConfigured(
        "SECRET_KEY must be a unique, secret value of at least 50 characters in production."
    )

ALLOWED_HOSTS = env("ALLOWED_HOSTS")
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set explicitly in production (no wildcard).")

# TD-11 / O-11: encrypted fields are unreadable without a valid Fernet key.
# Validate at process startup instead of discovering a typo on the first
# safeguarding/payment row read. Published development keys are never valid
# production secrets even though they have the right encoding and length.
_DEVELOPMENT_FIELD_ENCRYPTION_KEYS = {
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
    "c3RhcmZvcmdlLWRldi1maWVsZGVuYy1rZXktMzJieXQ=",
}
if not FIELD_ENCRYPTION_KEY or FIELD_ENCRYPTION_KEY in _DEVELOPMENT_FIELD_ENCRYPTION_KEYS:
    raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be a unique production Fernet key.")
try:
    Fernet(FIELD_ENCRYPTION_KEY.encode("ascii"))
except (TypeError, ValueError, UnicodeEncodeError) as exc:
    raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY must be a valid Fernet key.") from exc

# CORS/CSRF must be an explicit allowlist in prod, never a wildcard (D5-A-3):
# CORS_ALLOW_CREDENTIALS is True, so a wildcard origin would expose authenticated
# responses to any site.
if globals().get("CORS_ALLOW_ALL_ORIGINS"):
    raise ImproperlyConfigured("CORS_ALLOW_ALL_ORIGINS must be False in production.")
validate_exact_https_origins("CORS_ALLOWED_ORIGINS", CORS_ALLOWED_ORIGINS)
validate_exact_https_origins("CSRF_TRUSTED_ORIGINS", CSRF_TRUSTED_ORIGINS)
for _ws_origin in validate_exact_https_origins(
    "WEBSOCKET_ALLOWED_ORIGINS",
    WEBSOCKET_ALLOWED_ORIGINS,
):
    _parsed_ws_origin = urlsplit(_ws_origin)
    if _parsed_ws_origin.port not in (None, 443):
        raise ImproperlyConfigured("WEBSOCKET_ALLOWED_ORIGINS must use the default HTTPS port.")
if not 10 <= WEBSOCKET_HANDSHAKE_RATE_LIMIT <= 1000:
    raise ImproperlyConfigured("WEBSOCKET_HANDSHAKE_RATE_LIMIT must be between 10 and 1000.")
if not 2 <= WEBSOCKET_USER_CONNECT_RATE_LIMIT <= 120:
    raise ImproperlyConfigured("WEBSOCKET_USER_CONNECT_RATE_LIMIT must be between 2 and 120.")
if not 1 <= WEBSOCKET_MAX_CONNECTIONS_PER_SESSION <= 20:
    raise ImproperlyConfigured("WEBSOCKET_MAX_CONNECTIONS_PER_SESSION must be between 1 and 20.")
if not 60 <= WEBSOCKET_CONNECTION_LEASE_SECONDS <= 300:
    raise ImproperlyConfigured("WEBSOCKET_CONNECTION_LEASE_SECONDS must be between 60 and 300.")
if not 1 <= APP_AVAILABILITY_CACHE_TIMEOUT_SECONDS <= 300:
    raise ImproperlyConfigured("APP_AVAILABILITY_CACHE_TIMEOUT_SECONDS must be between 1 and 300.")

# Production terminates TLS behind a reverse proxy (SECURE_PROXY_SSL_HEADER below),
# so NUM_PROXIES MUST reflect the trusted hop count — otherwise client_ip / DRF's
# get_ident resolve every client to the proxy's IP and all IP-keyed throttles
# (login_ip, otp_ip, ...) collapse into one shared bucket.
if env("NUM_PROXIES") < 1:
    raise ImproperlyConfigured(
        "NUM_PROXIES must be set to the number of trusted reverse-proxy hops (>=1) in production; "
        "IP-keyed throttles depend on it."
    )
if not 1 <= SESSION_TTL_DAYS <= 30:
    raise ImproperlyConfigured("SESSION_TTL_DAYS must be between 1 and 30 in production.")
if not 5 <= SESSION_IDLE_TIMEOUT_MINUTES <= 24 * 60:
    raise ImproperlyConfigured("SESSION_IDLE_TIMEOUT_MINUTES must be between 5 and 1440 in production.")
if not 60 <= PRINT_AGENT_LEASE_SECONDS <= 60 * 60:
    raise ImproperlyConfigured("PRINT_AGENT_LEASE_SECONDS must be between 60 and 3600 in production.")
if not 1 <= PRINT_STALE_LEASE_SWEEP_BATCH_SIZE <= 1000:
    raise ImproperlyConfigured("PRINT_STALE_LEASE_SWEEP_BATCH_SIZE must be between 1 and 1000 in production.")

# Abuse controls are security policy, not advisory tuning. Reject malformed,
# zero, negative, unknown-period, and unreasonably large values at process
# startup instead of discovering them on the first production request.
for _rate_setting_name in (
    "HEALTH_READY_RATELIMIT",
    "ADMIN_LOGIN_RATELIMIT",
    "API_RATELIMIT_PREAUTH",
    "API_RATELIMIT_ANON",
    "API_RATELIMIT_USER",
    "API_RATELIMIT_AGENT",
):
    try:
        parse_rate(globals()[_rate_setting_name], setting_name=_rate_setting_name)
    except RateConfigurationError as exc:
        raise ImproperlyConfigured(str(exc)) from exc


def _require_service_url(name: str, *, schemes: tuple[str, ...]) -> str:
    value = env(name)
    parsed = urlsplit(value)
    if not value or parsed.scheme not in schemes or not parsed.hostname:
        raise ImproperlyConfigured(f"{name} must be an explicit production service URL.")
    return value


def _require_provider_https_url(name: str, value: str, allowed_hosts: list[str]) -> str:
    """Fail startup unless a credential-bearing provider uses one exact TLS host."""

    from infrastructure.http_client import InvalidProviderEndpoint, validate_https_endpoint

    if not allowed_hosts:
        raise ImproperlyConfigured(f"{name} requires a non-empty exact hostname allowlist.")
    try:
        return validate_https_endpoint(value, allowed_hosts=allowed_hosts)
    except InvalidProviderEndpoint as exc:
        raise ImproperlyConfigured(
            f"{name} must be an allowlisted HTTPS provider URL without credentials or redirects."
        ) from exc


# Never inherit base.py's developer services or published local credentials in
# production. A misspelled secret must stop the release, not connect the API to
# an unrelated localhost database/cache.
_require_service_url("DATABASE_URL", schemes=("postgres", "postgresql"))
_require_service_url("REDIS_URL", schemes=("redis", "rediss"))
_require_provider_https_url("CLICK_CHECKOUT_URL", CLICK_CHECKOUT_URL, CLICK_CHECKOUT_ALLOWED_HOSTS)
_require_provider_https_url("PAYME_CHECKOUT_URL", PAYME_CHECKOUT_URL, PAYME_CHECKOUT_ALLOWED_HOSTS)
_dns_verification_url = urlsplit(DOMAIN_VERIFICATION_DNS_URL)
_dns_verification_host = (_dns_verification_url.hostname or "").lower().rstrip(".")
try:
    _dns_verification_port = _dns_verification_url.port
except ValueError as exc:
    raise ImproperlyConfigured("DOMAIN_VERIFICATION_DNS_URL contains an invalid port.") from exc
try:
    ipaddress.ip_address(_dns_verification_host)
except ValueError:
    _dns_verification_host_is_ip = False
else:
    _dns_verification_host_is_ip = True
if (
    _dns_verification_url.scheme != "https"
    or not DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS
    or _dns_verification_host_is_ip
    or _dns_verification_host
    not in {host.strip().lower().rstrip(".") for host in DOMAIN_VERIFICATION_DNS_ALLOWED_HOSTS}
    or _dns_verification_url.username is not None
    or _dns_verification_url.password is not None
    or _dns_verification_url.query
    or _dns_verification_url.fragment
    or _dns_verification_port not in (None, 443)
):
    raise ImproperlyConfigured(
        "DOMAIN_VERIFICATION_DNS_URL must be an allowlisted HTTPS DNS-over-HTTPS endpoint."
    )
_media_bucket_name = env("AWS_STORAGE_BUCKET_NAME")
_static_bucket_name = env("AWS_STATIC_BUCKET_NAME")
if not _media_bucket_name or not _static_bucket_name or _media_bucket_name == _static_bucket_name:
    raise ImproperlyConfigured(
        "AWS_STORAGE_BUCKET_NAME and AWS_STATIC_BUCKET_NAME must name distinct production buckets."
    )


def _validate_storage_credentials(access_key_name: str, secret_key_name: str) -> tuple[str, str]:
    access_key = env(access_key_name).strip()
    secret_key = env(secret_key_name)
    if (
        not 3 <= len(access_key) <= 128
        or access_key.startswith(("REPLACE_", "GENERATE_"))
        or any(
            character.isspace() or ord(character) < 33 or ord(character) == 127 for character in access_key
        )
        or len(secret_key) < 32
        or secret_key.startswith(("REPLACE_", "GENERATE_"))
        or any(ord(character) < 33 or ord(character) == 127 for character in secret_key)
    ):
        raise ImproperlyConfigured(
            f"{access_key_name} and {secret_key_name} must contain an explicit strong service credential."
        )
    return access_key, secret_key


if env("AWS_EC2_METADATA_DISABLED", default="").strip().casefold() != "true":
    raise ImproperlyConfigured(
        "AWS_EC2_METADATA_DISABLED must be true so object storage never falls back to instance metadata."
    )

_static_storage_write_enabled = env.bool("STATIC_STORAGE_WRITE_ENABLED", default=False)
STATIC_STORAGE_WRITE_ENABLED = _static_storage_write_enabled
_media_storage_access_key = env("AWS_S3_ACCESS_KEY_ID", default="")
_media_storage_secret_key = env("AWS_S3_SECRET_ACCESS_KEY", default="")
_static_storage_access_key = env("AWS_STATIC_ACCESS_KEY_ID", default="")
_static_storage_secret_key = env("AWS_STATIC_SECRET_ACCESS_KEY", default="")
if _static_storage_write_enabled:
    if _media_storage_access_key or _media_storage_secret_key:
        raise ImproperlyConfigured(
            "The isolated collectstatic process must not receive media-runtime credentials."
        )
    _static_storage_access_key, _static_storage_secret_key = _validate_storage_credentials(
        "AWS_STATIC_ACCESS_KEY_ID", "AWS_STATIC_SECRET_ACCESS_KEY"
    )
    STORAGES["default"] = {  # noqa: F405
        "BACKEND": "infrastructure.storage.backends.DisabledObjectStorage",
    }
else:
    _media_storage_access_key, _media_storage_secret_key = _validate_storage_credentials(
        "AWS_S3_ACCESS_KEY_ID", "AWS_S3_SECRET_ACCESS_KEY"
    )
    # Rebind the runtime backend to the validated explicit pair. The absence of
    # a credential-provider fallback is visible in the final settings object,
    # not merely an import-time assertion.
    STORAGES["default"]["OPTIONS"]["access_key"] = _media_storage_access_key  # type: ignore[index]  # noqa: F405
    STORAGES["default"]["OPTIONS"]["secret_key"] = _media_storage_secret_key  # type: ignore[index]  # noqa: F405
    if _static_storage_access_key or _static_storage_secret_key:
        raise ImproperlyConfigured(
            "Static-writer credentials must not be present in a long-running production process."
        )


def _validate_storage_public_origin(name: str, value: str):
    validate_exact_https_origins(name, (value,))
    parsed = urlsplit(value)
    if parsed.hostname in {"localhost", "minio", "127.0.0.1", "::1"}:
        raise ImproperlyConfigured(f"{name} must be a browser-reachable HTTPS origin without credentials.")
    return parsed


AWS_S3_PUBLIC_ENDPOINT_URL = _require_service_url("AWS_S3_PUBLIC_ENDPOINT_URL", schemes=("https",)).rstrip(
    "/"
)
AWS_STATIC_PUBLIC_ENDPOINT_URL = _require_service_url(
    "AWS_STATIC_PUBLIC_ENDPOINT_URL", schemes=("https",)
).rstrip("/")
_storage_public_url = _validate_storage_public_origin(
    "AWS_S3_PUBLIC_ENDPOINT_URL", AWS_S3_PUBLIC_ENDPOINT_URL
)
_static_public_url = _validate_storage_public_origin(
    "AWS_STATIC_PUBLIC_ENDPOINT_URL", AWS_STATIC_PUBLIC_ENDPOINT_URL
)
if _storage_public_url.netloc.casefold() == _static_public_url.netloc.casefold():
    raise ImproperlyConfigured("Static assets and user-controlled media must use different public origins.")
if EMAIL_ENABLED:
    _email_host = env("EMAIL_HOST").strip().lower()
    _email_port = env.int("EMAIL_PORT")
    _email_user = env("EMAIL_HOST_USER")
    _email_password = env("EMAIL_HOST_PASSWORD")
    if _email_host in {"", "localhost", "127.0.0.1", "::1"}:
        raise ImproperlyConfigured("EMAIL_HOST must be configured explicitly in production.")
    if not 1 <= _email_port <= 65535:
        raise ImproperlyConfigured("EMAIL_PORT must be between 1 and 65535 in production.")
    if not env.bool("EMAIL_USE_TLS"):
        raise ImproperlyConfigured(
            "EMAIL_USE_TLS must be enabled when email delivery is enabled in production."
        )
    if bool(_email_user) != bool(_email_password):
        raise ImproperlyConfigured("EMAIL_HOST_USER and EMAIL_HOST_PASSWORD must be supplied together.")

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# Bearer credentials must never appear in URLs, access logs, browser history, or
# reverse-proxy telemetry. WebSocket clients authenticate with the negotiated
# ``bearer.<token>`` subprotocol instead.
WEBSOCKET_ALLOW_QUERY_TOKEN = False
HEALTH_READY_CACHE_SECONDS = 2.0
HEALTH_REQUIRE_CELERY_HEARTBEAT = True

# Django 6 ships a native CSP middleware. Keep the API/admin baseline strict;
# inline styles remain allowed for Django admin compatibility, while scripts,
# frames, plugins, and form targets stay same-origin/fail-closed.
MIDDLEWARE.insert(  # noqa: F405
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,  # noqa: F405
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
)
_storage_public_origin = f"{_storage_public_url.scheme}://{_storage_public_url.netloc}"
_static_public_origin = f"{_static_public_url.scheme}://{_static_public_url.netloc}"
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "base-uri": [CSP.SELF],
    "object-src": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
    "script-src": [CSP.SELF, _static_public_origin],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE, _static_public_origin],
    "img-src": [CSP.SELF, "data:", _static_public_origin, _storage_public_origin],
    "font-src": [CSP.SELF, "data:", _static_public_origin],
    "connect-src": [CSP.SELF, _storage_public_origin],
}

# Never mock real SMS in prod.
ESKIZ_USE_MOCK = False
SMS_MOCK_CAPTURE_OUTBOX = False

# Never mock the Anthropic API in prod (D4-LA-2). Requires a real key [OWNER:O-2].
ANTHROPIC_USE_MOCK = False

# Never ship a mock money/fiscal/push integration to prod. base.py defaults these
# to True (mock-first, TD-2) and only ESKIZ/ANTHROPIC were forced off here, so a
# misconfigured prod could silently fake payments/fiscalization/push. Force them
# all real — real provider credentials are then required [OWNER:O-5/O-7].
CLICK_USE_MOCK = False
PAYME_USE_MOCK = False
UZUM_USE_MOCK = False
# The repository's legacy single-HMAC callback is not Uzum's current official
# Basic-auth /check,/create,/confirm,/reverse,/status Merchant API. Never expose
# that compatibility path in a production process.
UZUM_LEGACY_INTEGRATION_ENABLED = False
FINANCE_FX_USE_MOCK = False
SOLIQ_USE_MOCK = False
FCM_USE_MOCK = False
FCM_MOCK_CAPTURE_OUTBOX = False
ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS = False
ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS = False
PLATFORM_PAYMENTS_USE_MOCK = False


def _require_credentials(integration: str, *names: str) -> None:
    missing = [name for name in names if not str(env(name)).strip()]
    if missing:
        raise ImproperlyConfigured(
            f"{integration} is enabled in production but required credentials are missing: "
            + ", ".join(missing)
        )


def _validate_firebase_credentials() -> None:
    path = Path(env("FCM_CREDENTIALS_FILE"))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImproperlyConfigured(
            "Firebase push is enabled but FCM_CREDENTIALS_FILE is not readable service-account JSON."
        ) from exc
    required = {"type", "project_id", "private_key", "client_email"}
    if payload.get("type") != "service_account" or not all(payload.get(key) for key in required):
        raise ImproperlyConfigured("Firebase push is enabled but its service-account JSON is incomplete.")


if SMS_ENABLED:
    _require_credentials("Eskiz SMS", "ESKIZ_EMAIL", "ESKIZ_PASSWORD", "ESKIZ_FROM")
    _require_provider_https_url("ESKIZ_API_URL", ESKIZ_API_URL, ESKIZ_API_ALLOWED_HOSTS)
if AI_ENABLED:
    _require_credentials("Anthropic AI", "ANTHROPIC_API_KEY")
if FISCALIZATION_ENABLED:
    _require_credentials("Soliq fiscalization", "SOLIQ_API_URL", "SOLIQ_API_TOKEN")
    _require_provider_https_url("SOLIQ_API_URL", SOLIQ_API_URL, SOLIQ_API_ALLOWED_HOSTS)
if PUSH_NOTIFICATIONS_ENABLED:
    _require_credentials("Firebase push", "FCM_CREDENTIALS_FILE")
    _validate_firebase_credentials()

# Structured JSON logging in production only (D1-LA-10) — dev/test stay human.
LOGGING["formatters"]["json"] = {  # type: ignore[index]  # noqa: F405
    "()": "core.logging_filters.JsonFormatter",
}
LOGGING["handlers"]["console"]["formatter"] = "json"  # type: ignore[index]  # noqa: F405
# Debug logs are useful locally but materially increase the chance that a future
# integration logs request/provider detail. Production keeps the reviewed INFO
# boundary and the formatter applies a final credential/PII redaction pass.
LOGGING["loggers"]["starforge"]["level"] = "INFO"  # type: ignore[index]  # noqa: F405

# Sentry — config-only (D1-LA-13 / O-10). No effect unless SENTRY_DSN is set,
# so dev/test/CI never need the dependency installed.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        send_default_pii=False,
        environment=env("SENTRY_ENVIRONMENT", default="production"),
    )

# Long-running processes use a URL-only backend and therefore have no S3 client
# or credential-provider chain for the CSP-trusted static origin.  Only the
# isolated Compose ``collectstatic`` tool receives static-storage.env and sets
# STATIC_STORAGE_WRITE_ENABLED=True.
if _static_storage_write_enabled:
    STORAGES["staticfiles"] = {  # noqa: F405
        "BACKEND": "infrastructure.storage.backends.DualEndpointS3Storage",
        "OPTIONS": {
            "bucket_name": _static_bucket_name,
            "endpoint_url": env("AWS_S3_ENDPOINT_URL") or None,
            "access_key": _static_storage_access_key,
            "secret_key": _static_storage_secret_key,
            "region_name": env("AWS_S3_REGION_NAME"),
            "addressing_style": "path",
            "signature_version": "s3v4",
            "querystring_auth": False,
            "public_endpoint_url": AWS_STATIC_PUBLIC_ENDPOINT_URL,
        },
    }
else:
    STORAGES["staticfiles"] = {  # noqa: F405
        "BACKEND": "infrastructure.storage.backends.PublicStaticFilesStorage",
        "OPTIONS": {
            "bucket_name": _static_bucket_name,
            "public_endpoint_url": AWS_STATIC_PUBLIC_ENDPOINT_URL,
        },
    }
