"""Staging settings — production-shaped without billable provider traffic."""

import os

# Production settings fail fast when real-provider credentials are absent. A staging
# deployment deliberately uses mocks, so supply non-secret placeholders solely while
# importing the shared hardening settings; every integration is forced back to its mock
# implementation below before Django starts serving requests.
_STAGING_PLACEHOLDERS = {
    "ESKIZ_EMAIL": "staging-mock@example.invalid",
    "ESKIZ_PASSWORD": "staging-mock",
    "ESKIZ_FROM": "staging",
    "ANTHROPIC_API_KEY": "staging-mock",
    "SOLIQ_API_URL": "https://soliq-staging.invalid",
    "SOLIQ_API_ALLOWED_HOSTS": "soliq-staging.invalid",
    "SOLIQ_API_TOKEN": "staging-mock",
    "FCM_CREDENTIALS_FILE": "/run/secrets/staging-firebase-mock.json",
}
for _name, _value in _STAGING_PLACEHOLDERS.items():
    os.environ.setdefault(_name, _value)

from .production import *  # noqa: E402,F403

DEBUG = False
ESKIZ_USE_MOCK = True
SMS_MOCK_CAPTURE_OUTBOX = False
ANTHROPIC_USE_MOCK = True
CLICK_USE_MOCK = True
PAYME_USE_MOCK = True
UZUM_USE_MOCK = True
UZUM_LEGACY_INTEGRATION_ENABLED = False
FINANCE_FX_USE_MOCK = True
SOLIQ_USE_MOCK = True
FCM_USE_MOCK = True
FCM_MOCK_CAPTURE_OUTBOX = False
ALLOW_LEGACY_PRINCIPAL_UNION_FOR_TESTS = False
ALLOW_LEGACY_TENANT_SESSIONS_FOR_TESTS = False
PLATFORM_PAYMENTS_USE_MOCK = True
LOGGING["loggers"][""]["level"] = "DEBUG"  # type: ignore[index]  # noqa: F405
