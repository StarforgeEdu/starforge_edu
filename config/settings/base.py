"""
Base settings shared across all environments.

Tenancy: schema-per-tenant via django-tenants. Center is the tenant model;
Domain maps a hostname (subdomain) to a Center. The public schema holds
shared/platform-level data; each tenant schema holds the per-center data.

Auth: custom session auth (core.session_auth) — an opaque ``Session.key`` Bearer
token validated against a per-tenant ``Session`` row (no JWT library). Django's
cookie sessions remain enabled only so the built-in /admin/ keeps working.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    SECRET_KEY=(str, "dev-only-CHANGE-ME"),
    ALLOWED_HOSTS=(list, ["*"]),
    DATABASE_URL=(str, "postgres://starforge:starforge@localhost:5432/starforge"),
    REDIS_URL=(str, "redis://localhost:6379/0"),
    CELERY_BROKER_URL=(str, ""),
    CELERY_RESULT_BACKEND=(str, ""),
    CHANNEL_REDIS_URL=(str, ""),
    CORS_ALLOWED_ORIGINS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    AWS_STORAGE_BUCKET_NAME=(str, "starforge-media"),
    AWS_S3_ENDPOINT_URL=(str, ""),
    AWS_S3_ACCESS_KEY_ID=(str, ""),
    AWS_S3_SECRET_ACCESS_KEY=(str, ""),
    AWS_S3_REGION_NAME=(str, "us-east-1"),
    ESKIZ_API_URL=(str, "https://notify.eskiz.uz/api"),
    ESKIZ_EMAIL=(str, ""),
    ESKIZ_PASSWORD=(str, ""),
    ESKIZ_FROM=(str, "4546"),  # TD-17: approved sender nick; 4546 is Eskiz's test sender
    ESKIZ_USE_MOCK=(bool, True),
    NUM_PROXIES=(int, 0),  # trusted reverse-proxy hops for X-Forwarded-For (0 = trust REMOTE_ADDR only)
    ANTHROPIC_API_KEY=(str, ""),
    ANTHROPIC_USE_MOCK=(bool, True),  # D4-LA-2 (TD-2): mock-first; production.py sets False
    FIELD_ENCRYPTION_KEY=(str, ""),  # TD-11 Fernet key (O-11); dev/test override locally
    DEFAULT_FROM_EMAIL=(str, "noreply@starforge.uz"),
    EMAIL_HOST=(str, "localhost"),
    EMAIL_PORT=(int, 25),
    EMAIL_HOST_USER=(str, ""),
    EMAIL_HOST_PASSWORD=(str, ""),
    EMAIL_USE_TLS=(bool, False),
    # --- Day 3: payment providers (TD-6), mock-first (TD-2). Per-tenant merchant
    # credentials live encrypted in payments.ProviderConfig; these are toggles +
    # redirect bases only. ---
    CLICK_USE_MOCK=(bool, True),
    CLICK_CHECKOUT_URL=(str, "https://my.click.uz/services/pay"),
    PAYME_USE_MOCK=(bool, True),
    PAYME_CHECKOUT_URL=(str, "https://checkout.paycom.uz"),
    UZUM_USE_MOCK=(bool, True),
    UZUM_CHECKOUT_URL=(str, "https://www.uzumbank.uz/open-service"),
    # --- Soliq fiscalization (TD-7), mock-first [OWNER:O-5] ---
    SOLIQ_USE_MOCK=(bool, True),
    SOLIQ_API_URL=(str, ""),
    SOLIQ_API_TOKEN=(str, ""),
    SOLIQ_QR_BASE_URL=(str, "https://ofd.soliq.uz/check"),
    # --- FCM push (TD-15), mock-first [OWNER:O-7] ---
    FCM_USE_MOCK=(bool, True),
    FCM_CREDENTIALS_FILE=(str, ""),
    # --- Billing / paywall (TD-8) ---
    BILLING_TRIAL_GRACE_DAYS=(int, 3),
    BILLING_DUNNING_DAYS=(int, 7),
    # Platform (owner) merchant credentials for subscription checkout, mock-first.
    PLATFORM_PAYMENTS_USE_MOCK=(bool, True),
    PLATFORM_CLICK_SERVICE_ID=(str, ""),
    PLATFORM_CLICK_MERCHANT_ID=(str, ""),
    PLATFORM_CLICK_SECRET_KEY=(str, ""),
    PLATFORM_PAYME_MERCHANT_ID=(str, ""),
    PLATFORM_PAYME_KEY=(str, ""),
)

env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# ---------------------------------------------------------------------------
# Apps: SHARED_APPS (public schema) vs TENANT_APPS (per-tenant schema)
# ---------------------------------------------------------------------------
# django-tenants requires this split. apps that appear in both will have a
# table in the public schema AND in every tenant schema. Center/Domain live
# only in public; every domain app lives only in tenants.

SHARED_APPS = [
    "django_tenants",
    "apps.tenancy.apps.TenancyConfig",  # Center + Domain only (the tenant model)
    "django.contrib.contenttypes",
    "django.contrib.auth",
    # Custom admin config: auto-registers EVERY model after autodiscovery so nothing is missing
    # from /admin/ (still the standard admin app + site — see core/admin_apps.py).
    "core.admin_apps.StarforgeAdminConfig",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Required for schedule.Lesson's GiST ExclusionConstraint (no models, so it
    # lives in SHARED_APPS only) — silences postgres.E005 so a fresh `migrate`
    # passes the system check instead of aborting.
    "django.contrib.postgres",
    # TD-3 / ADR-007: identity also lives in the public schema so platform staff
    # can log into the apex /admin/ and IsAdminUser works on the platform API.
    # These stay in TENANT_APPS too (a table per tenant schema as well).
    "apps.users.apps.UsersConfig",
    "apps.auth.apps.AuthAppConfig",
    # TD-8: platform billing (Plan/Subscription/UsageSnapshot) is public-schema
    # only — it monetizes tenants, so it must NOT appear in TENANT_APPS.
    "apps.billing.apps.BillingConfig",
    "rest_framework_simplejwt.token_blacklist",
    "django_celery_beat",
    "channels",
    "corsheaders",
]

TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "core.admin_apps.StarforgeAdminConfig",  # auto-registers every model (see SHARED_APPS)
    "django.contrib.sessions",
    "django.contrib.messages",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_filters",
    "apps.users.apps.UsersConfig",
    "apps.auth.apps.AuthAppConfig",
    "apps.org.apps.OrgConfig",  # Branch + Department (per-tenant org structure)
    "apps.students.apps.StudentsConfig",
    "apps.parents.apps.ParentsConfig",
    "apps.teachers.apps.TeachersConfig",
    "apps.cohorts.apps.CohortsConfig",
    "apps.schedule.apps.ScheduleConfig",
    "apps.attendance.apps.AttendanceConfig",
    "apps.academics.apps.AcademicsConfig",
    "apps.assignments.apps.AssignmentsConfig",
    "apps.content.apps.ContentConfig",
    "apps.printing.apps.PrintingConfig",
    "apps.finance.apps.FinanceConfig",
    "apps.payments.apps.PaymentsConfig",
    "apps.notifications.apps.NotificationsConfig",
    "apps.ai.apps.AIConfig",
    "apps.audit.apps.AuditConfig",
    "apps.reports.apps.ReportsConfig",
    "apps.approvals.apps.ApprovalsConfig",  # A-1: approvals + ledger engine
    "apps.compliance.apps.ComplianceConfig",  # rule book / policy acknowledgment (#12)
    "apps.access.apps.AccessConfig",  # A-2: dynamic, center-configurable permissions
    "apps.forms.apps.FormsConfig",  # F3-3: forms / surveys engine
    "apps.tasks.apps.TasksConfig",  # F5: tasks + role hierarchy
    "apps.messaging.apps.MessagingConfig",  # F4-4: in-app messaging
    "apps.intelligence.apps.IntelligenceConfig",  # A-3: risk flags / intelligence
    "apps.achievements.apps.AchievementsConfig",  # F15-2: custom achievements
    "apps.rewards.apps.RewardsConfig",  # F17-1: staff rewards
    "apps.covers.apps.CoversConfig",  # F18-1: lesson cover requests
    "apps.loans.apps.LoansConfig",  # F21-1: staff loans (A-1 kind + repayments)
    "apps.procurement.apps.ProcurementConfig",  # #15: procurement / purchase orders (A-1 kind)
    "apps.campaigns.apps.CampaignsConfig",  # F10-1: SMS campaigns to student segments
    "apps.sales.apps.SalesConfig",  # #8: book/material cash sales (money-IN ledger)
    "apps.meetings.apps.MeetingsConfig",  # F3-5: staff meetings + RSVP
    "apps.placement.apps.PlacementConfig",  # F1-2/F1-4: placement tests + approval
    "apps.cards.apps.CardsConfig",  # F12-1: student ID/access cards + scan check-in
]

INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = "tenancy.Center"
TENANT_DOMAIN_MODEL = "tenancy.Domain"
PUBLIC_SCHEMA_URLCONF = "config.urls_public"

# ---------------------------------------------------------------------------
# Middleware (TenantMainMiddleware MUST be first)
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    # Outermost: stamps every request/response with an X-Request-ID and exposes
    # it to log records — must wrap everything, including the health probes.
    "core.middleware.RequestIDMiddleware",
    # Backend API: rewrite ANY HTML error response (unmatched URL, non-DRF 500,
    # admin, DEBUG technical pages) into the JSON {"error": {...}} envelope. Just
    # below RequestID so it runs late outbound and preserves inner CORS headers.
    "core.middleware.JsonErrorResponseMiddleware",
    # Liveness/readiness probes answer on ANY Host header and must bypass tenant
    # resolution, so this sits before TenantMainMiddleware (D1-LA-8).
    "core.middleware.HealthCheckMiddleware",
    # Blanket /api/ rate cap for BOTH view styles (plain FBVs bypass DRF's
    # throttles). Before tenant resolution so a flood never costs a schema lookup.
    "core.middleware.ApiRateLimitMiddleware",
    "django_tenants.middleware.main.TenantMainMiddleware",
    # CORS must wrap the short-circuit responses below (402 paywall / 503 inactive)
    # so a browser SPA on an allowed origin can read the real envelope instead of a
    # generic CORS failure — hence it sits ABOVE SubscriptionGate/InactiveTenant.
    "corsheaders.middleware.CorsMiddleware",
    # TD-8 paywall: a suspended tenant's API returns 402 (needs the resolved
    # tenant, so immediately after TenantMainMiddleware; allowlists admin/auth/
    # healthz/schema; public schema is a no-op).
    "apps.billing.middleware.SubscriptionGateMiddleware",
    # A resolved-but-inactive tenant returns 503 (Lane B, after tenant resolution).
    "core.middleware.InactiveTenantMiddleware",
    # Per-app fault isolation: a disabled app (or one whose hard dependency is down)
    # answers a clean 503 so one app never falls the whole API; a degraded app (soft
    # dependency down) is served with a `warnings` list. Runs after tenant resolution.
    "core.middleware.AppAvailabilityMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database (django-tenants postgresql backend)
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db_url("DATABASE_URL"),
}
DATABASES["default"]["ENGINE"] = "django_tenants.postgresql_backend"
# Reuse a Postgres connection across requests instead of opening a fresh one each time
# (Django's default CONN_MAX_AGE=0). On a small box the per-request connect + the
# django-tenants ``SET search_path`` is a measurable slice of DB CPU under load;
# persistent connections cut it. Paired with CONN_HEALTH_CHECKS so a dropped/idle-killed
# connection is detected and replaced at the start of the next request (Django 4.1+),
# never handing a stale socket to a view. Overridable via env; tests pin it to 0.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DATABASE_ROUTERS = ["django_tenants.routers.TenantSyncRouter"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "users.User"
AUTHENTICATION_BACKENDS = [
    "apps.auth.backends.PhoneOrEmailBackend",
    "django.contrib.auth.backends.ModelBackend",  # for /admin/
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# DRF + simplejwt + drf-spectacular
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Custom session auth (no JWT): opaque Session.key Bearer token, tenant-bound
        # by the schema it lives in. (Django's cookie SessionAuthentication stays as a
        # fallback for the admin; the API is pure Bearer.)
        "core.session_auth.SessionAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # Backend JSON API (no HTML browsable-API UI): respond JSON only. The OpenAPI
    # schema + Swagger/Redoc stay available as API docs for the mobile/web clients.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "core.pagination.DefaultPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",  # keep in sync with API_RATELIMIT_ANON below
        "user": "1000/min",  # keep in sync with API_RATELIMIT_USER below
        "login_user": "5/min",
        "login_ip": "10/min",
        "otp_phone": "3/min",
        "otp_verify": "10/min",
        "otp_ip": "10/min",
        "otp_global": "1000/hour",
        # Per-(schema, user) caps on expensive endpoints (core.throttles).
        "announcement": "10/min",
        "bulk_import": "6/min",
        "ai_generation": "20/min",
    },
    # Trusted-proxy depth for DRF's get_ident (IP throttles); mirrors
    # core.utils.client_ip so all IP-keyed controls share one source.
    # 0 = trust REMOTE_ADDR only (None would mean "trust raw XFF" — unsafe).
    "NUM_PROXIES": env("NUM_PROXIES"),
    "EXCEPTION_HANDLER": "core.exceptions.drf_exception_handler",
}

NUM_PROXIES = env("NUM_PROXIES")

# Blanket /api/ rate caps enforced by core.middleware.ApiRateLimitMiddleware for
# BOTH view styles (DRF's throttles above only see DRF views). DRF-format strings;
# a Bearer-carrying request buckets per token at USER, anything else per IP at ANON.
API_RATELIMIT_USER = env.str("API_RATELIMIT_USER", default="1000/min")
API_RATELIMIT_ANON = env.str("API_RATELIMIT_ANON", default="60/min")

SIMPLE_JWT = {
    # Request auth is custom session auth (core.session_auth), NOT JWT. simplejwt
    # stays configured only for the schedule iCal-feed signed token (a calendar
    # subscription URL can't carry a session). SESSION_TTL_DAYS bounds real sessions.
    "ACCESS_TOKEN_LIFETIME": timedelta(days=env.int("ACCESS_TOKEN_DAYS", default=7)),
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Starforge Edu API",
    "DESCRIPTION": "Multi-tenant education platform backend.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {},
}

# ---------------------------------------------------------------------------
# Channels (Redis)
# ---------------------------------------------------------------------------
# Allow the WS ?token= auth fallback (default on for client compatibility). Set
# False to force subprotocol-only auth (keeps tokens out of proxy/access logs).
WEBSOCKET_ALLOW_QUERY_TOKEN = env.bool("WEBSOCKET_ALLOW_QUERY_TOKEN", default=True)

# Fault isolation (core.availability): app labels turned OFF at boot (ops default). A
# director can additionally toggle apps at runtime via /api/v1/org/system/apps/ (a
# cache-backed override). A disabled app's endpoints answer 503 without falling the rest.
DISABLED_APPS = env.list("DISABLED_APPS", default=[])

# The Redis connection URL as a SETTING (not just an env read at each use site): the
# readiness probe + task dead-letter queue go through infrastructure.cache.redis_client.
# get_redis(), which reads `settings.REDIS_URL` — but nothing defined it, so `settings.
# REDIS_URL` raised AttributeError → /healthz/ready always 503 "Cache unavailable" and the
# observability DLQ push 500'd. Same env() call the cache/broker already use successfully.
REDIS_URL = env("REDIS_URL")

_channel_redis = env("CHANNEL_REDIS_URL") or env("REDIS_URL")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [_channel_redis]},
    },
}

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL") or env("REDIS_URL")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND") or env("REDIS_URL")
CELERY_TIMEZONE = "Asia/Tashkent"
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# tenant-schemas-celery: every task auto-activates the right tenant schema.
CELERY_TASK_CLS = "tenant_schemas_celery.task:TenantTask"

# Beat schedule (DatabaseScheduler ingests this at beat startup; tasks register
# with workers via celery_tasks/tasks.py — see tests/test_celery_registration.py.
# purge_expired_otps already iterates public + tenant schemas; D4-F only
# consolidates schedule registration).
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    "deactivate-expired-trials": {
        "task": "celery_tasks.tenancy_tasks.deactivate_expired_trials",
        "schedule": 60 * 60,  # hourly
    },
    "purge-expired-otps": {
        "task": "celery_tasks.cleanup_tasks.purge_expired_otps",
        "schedule": 60 * 60 * 24,  # daily
    },
    "send-lesson-reminders": {
        "task": "celery_tasks.schedule_tasks.send_lesson_reminders",
        "schedule": 60 * 5,  # every 5 min (D2-A-7)
    },
    "archive-completed-terms": {
        "task": "celery_tasks.schedule_tasks.archive_completed_terms",
        "schedule": 60 * 60 * 24 * 7,  # weekly
    },
    "mark-absent-after-lesson": {
        "task": "celery_tasks.attendance_tasks.mark_absent_after_lesson",
        "schedule": 60 * 15,  # every 15 min (D2-B-7)
    },
    "send-due-soon-reminders": {
        "task": "celery_tasks.assignment_tasks.send_due_soon_reminders",
        "schedule": 60 * 60,  # hourly (D2-D-7)
    },
    "late-payment-reminders": {
        "task": "celery_tasks.finance_tasks.late_payment_reminders",
        "schedule": 60 * 60 * 24,  # daily (D3-A-8)
    },
    "cleanup-old-audit-logs": {
        "task": "celery_tasks.audit_tasks.cleanup_old_audit_logs",
        "schedule": 60 * 60 * 24 * 7,  # weekly (D3-D-6)
    },
    "run-nightly-metering": {
        "task": "celery_tasks.billing_tasks.run_nightly_metering",
        "schedule": 60 * 60 * 24,  # nightly usage snapshot + state flips (D3-E-5)
    },
    # Day 4 (D4-LF-4 consolidation)
    "flush-expired-jwt-blacklist": {
        "task": "celery_tasks.cleanup_tasks.flush_expired_jwt_blacklist",
        "schedule": 60 * 60 * 24 * 7,  # weekly
    },
    "run-due-report-schedules": {
        "task": "celery_tasks.report_tasks.run_due_report_schedules",
        # Clock-aligned hourly (:00) — schedule_is_due requires an exact
        # local.hour match, so a drifting fixed interval could skip an hour
        # bucket (and that hour's due schedules) after a beat restart (D4-LB-6).
        "schedule": crontab(minute=0),
    },
    "nightly-platform-aggregation": {
        "task": "celery_tasks.report_tasks.nightly_platform_aggregation",
        "schedule": 60 * 60 * 24,  # daily, public schema (D4-LB-7)
    },
    "dispatch-scheduled-campaigns": {
        "task": "celery_tasks.campaign_tasks.dispatch_scheduled_campaigns",
        "schedule": 60 * 5,  # every 5 min — send campaigns whose scheduled_at has arrived (F10-1)
    },
    "prune-webhook-events": {
        "task": "celery_tasks.payment_tasks.prune_webhook_events",
        "schedule": 60 * 60 * 24,  # daily — bound WebhookEvent storage growth (R6/CONF3)
    },
}

# ---------------------------------------------------------------------------
# Cache (Redis)
# ---------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": env("REDIS_URL"),
    },
}

# ---------------------------------------------------------------------------
# Storage (S3-compatible: AWS S3 in prod, MinIO in dev)
# ---------------------------------------------------------------------------
STORAGES = {
    "default": {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env("AWS_STORAGE_BUCKET_NAME"),
            "endpoint_url": env("AWS_S3_ENDPOINT_URL") or None,
            "access_key": env("AWS_S3_ACCESS_KEY_ID"),
            "secret_key": env("AWS_S3_SECRET_ACCESS_KEY"),
            "region_name": env("AWS_S3_REGION_NAME"),
            "addressing_style": "path",
            "signature_version": "s3v4",
            "file_overwrite": False,
        },
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# ---------------------------------------------------------------------------
# CORS / CSRF
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

# ---------------------------------------------------------------------------
# I18N / locale (uz primary, en secondary, ru tertiary)
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "uz"
LANGUAGES = [
    ("uz", "O‘zbekcha"),
    ("en", "English"),
    ("ru", "Русский"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Asia/Tashkent"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST")
EMAIL_PORT = env("EMAIL_PORT")
EMAIL_HOST_USER = env("EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
EMAIL_USE_TLS = env("EMAIL_USE_TLS")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} [tenant={schema} req={request_id}] {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {name} {message}",
            "style": "{",
        },
    },
    "filters": {
        "tenant": {"()": "core.logging_filters.TenantSchemaFilter"},
        "request_id": {"()": "core.logging_filters.RequestIDFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["tenant", "request_id"],
            "formatter": "verbose",
        },
    },
    "loggers": {
        "": {"handlers": ["console"], "level": "INFO"},
        "django.db.backends": {"level": "WARNING"},
        "starforge": {"handlers": ["console"], "level": "DEBUG", "propagate": False},
    },
}

# ---------------------------------------------------------------------------
# 3rd-party integration config
# ---------------------------------------------------------------------------
ESKIZ_API_URL = env("ESKIZ_API_URL")
ESKIZ_EMAIL = env("ESKIZ_EMAIL")
ESKIZ_PASSWORD = env("ESKIZ_PASSWORD")
ESKIZ_FROM = env("ESKIZ_FROM")  # TD-17: sender ID (was hardcoded "4546")
ESKIZ_USE_MOCK = env("ESKIZ_USE_MOCK")

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"
# D4-LA-2 (TD-2): when on, infrastructure/ai/anthropic_client.complete() returns
# a deterministic mock + fake usage with ZERO HTTP. Default True outside
# production; production.py forces it False (real key required, [OWNER:O-2]).
ANTHROPIC_USE_MOCK = env("ANTHROPIC_USE_MOCK")

# TD-11 field encryption (O-11). Empty by default; dev/test set a deterministic
# throwaway key, prod REQUIRES a real one (core.fields raises without it).
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY")
ANTHROPIC_PROMPT_CACHE_TTL_SECONDS = 60 * 60 * 24  # 24h
# Explicit HTTP timeout for the real Anthropic client (well under the 25-min task
# soft limit) so a stuck call fails fast and the task can retry.
ANTHROPIC_REQUEST_TIMEOUT_SECONDS = 120.0

# Per-tenant AI budget defaults (override per-tenant via TenantAIBudget rows).
AI_DEFAULT_DAILY_TOKENS = 100_000
AI_DEFAULT_MONTHLY_TOKENS = 2_000_000

# D4-LA-2/4 placeholder AI pricing (microUSD per million tokens). Real pricing is
# [OWNER:O-2]; apps.ai.services.cost_microusd() reads these (TD-13: no magic
# numbers). Defaults approximate Claude Sonnet list pricing ($3/MTok in,
# $15/MTok out) expressed in microUSD.
AI_COST_PER_MTOK_INPUT_MICROUSD = 3_000_000
AI_COST_PER_MTOK_OUTPUT_MICROUSD = 15_000_000

# OTP config (consumed by apps.auth)
# OTP codes serve password reset / contact verification only — login is
# username+password (owner decision 2026-06-11; see apps/auth/services.py).
OTP_LENGTH = 6
OTP_TTL_SECONDS = 5 * 60
OTP_MAX_ATTEMPTS = 5
# Resend cooldown + per-IP enumeration cap (CenterSettings overrides the
# cooldown per tenant — these are the platform fallbacks).
OTP_COOLDOWN_SECONDS = 60
OTP_IP_DISTINCT_IDENTIFIER_CAP = 5
OTP_IDENTIFIER_RATE_LIMIT = env.int("OTP_IDENTIFIER_RATE_LIMIT", default=3)
OTP_IDENTIFIER_RATE_WINDOW_SECONDS = env.int("OTP_IDENTIFIER_RATE_WINDOW_SECONDS", default=60)
OTP_GLOBAL_RATE_LIMIT = env.int("OTP_GLOBAL_RATE_LIMIT", default=1000)
OTP_GLOBAL_RATE_WINDOW_SECONDS = env.int("OTP_GLOBAL_RATE_WINDOW_SECONDS", default=60 * 60)

# ---------------------------------------------------------------------------
# Day 3: payment providers, fiscalization, push, billing (all mock-first, TD-2)
# ---------------------------------------------------------------------------
# Per-tenant merchant credentials live encrypted in payments.ProviderConfig;
# these settings are the mock toggles + provider redirect/checkout bases.
CLICK_USE_MOCK = env("CLICK_USE_MOCK")
CLICK_CHECKOUT_URL = env("CLICK_CHECKOUT_URL")
PAYME_USE_MOCK = env("PAYME_USE_MOCK")
PAYME_CHECKOUT_URL = env("PAYME_CHECKOUT_URL")
UZUM_USE_MOCK = env("UZUM_USE_MOCK")
UZUM_CHECKOUT_URL = env("UZUM_CHECKOUT_URL")

# Soliq e-fiscalization (TD-7) [OWNER:O-5]
SOLIQ_USE_MOCK = env("SOLIQ_USE_MOCK")
SOLIQ_API_URL = env("SOLIQ_API_URL")
SOLIQ_API_TOKEN = env("SOLIQ_API_TOKEN")
SOLIQ_QR_BASE_URL = env("SOLIQ_QR_BASE_URL")

# FCM push (TD-15) [OWNER:O-7]
FCM_USE_MOCK = env("FCM_USE_MOCK")
FCM_CREDENTIALS_FILE = env("FCM_CREDENTIALS_FILE")

# Billing / paywall (TD-8)
BILLING_TRIAL_GRACE_DAYS = env("BILLING_TRIAL_GRACE_DAYS")
BILLING_DUNNING_DAYS = env("BILLING_DUNNING_DAYS")
PLATFORM_PAYMENTS_USE_MOCK = env("PLATFORM_PAYMENTS_USE_MOCK")
PLATFORM_CLICK_SERVICE_ID = env("PLATFORM_CLICK_SERVICE_ID")
PLATFORM_CLICK_MERCHANT_ID = env("PLATFORM_CLICK_MERCHANT_ID")
PLATFORM_CLICK_SECRET_KEY = env("PLATFORM_CLICK_SECRET_KEY")
PLATFORM_PAYME_MERCHANT_ID = env("PLATFORM_PAYME_MERCHANT_ID")
PLATFORM_PAYME_KEY = env("PLATFORM_PAYME_KEY")
