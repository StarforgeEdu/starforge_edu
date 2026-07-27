"""Test settings — fast, deterministic, no external services."""

import base64

from .base import *  # noqa: F403

DEBUG = False
SECRET_KEY = "test-only-not-secret"
ALLOWED_HOSTS = ["*"]

# Deterministic field-encryption key (TD-11) so encrypted round-trips are stable.
FIELD_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"starforge-dev-fieldenc-key-32byt").decode()

# Synchronous Celery in tests.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
# Keep the real tenant-schemas-celery task base (config/celery.py wires
# core.celery_base:SchemaHeaderTask); CeleryApp ignores CELERY_TASK_CLS anyway.

# In-memory channel layer.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Locmem cache.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Local file storage in tests.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Always mock SMS in tests.
ESKIZ_USE_MOCK = True

# Faster password hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Fresh DB connection per test — persistent connections (base sets CONN_MAX_AGE=60)
# don't mix cleanly with pytest's per-test transaction wrapping + django-tenants schema
# switching, and tests want deterministic isolation over connection reuse.
DATABASES["default"]["CONN_MAX_AGE"] = 0  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = False  # noqa: F405

# Quiet logs in tests.
LOGGING["loggers"][""]["level"] = "WARNING"  # type: ignore[index]  # noqa: F405
