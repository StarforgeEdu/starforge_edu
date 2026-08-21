"""Public-schema URLConf — served on the bare apex domain.

Only platform-level concerns live here: the platform admin and the
endpoint(s) used to create new tenants. Everything tenant-specific
must go through the tenant URLConf in config/urls.py.
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularRedocView, SpectacularSwaggerView

from core.openapi import openapi_schema_view

urlpatterns = [
    path("admin/", admin.site.urls),
    # Platform staff need an API session before they can call /platform/*.
    # Role-native accounts remain tenant-only.
    path("api/v1/auth/", include("apps.auth.urls_public")),
    # OpenAPI schema + Swagger/Redoc for the PUBLIC (platform/billing/webhooks) API. The same
    # view serves the platform schema here because django-tenants points request.urlconf at
    # config.urls_public on the public host.
    path("api/schema/", openapi_schema_view, name="schema"),
    path("api/schema/swagger-ui/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    path("api/v1/platform/", include("apps.tenancy.urls")),
    # TD-8: platform billing API (plans/subscriptions/usage/checkout), staff-only.
    path("api/v1/platform/billing/", include("apps.billing.urls")),
    # D4-LE-3: flat control-center subscription management (by subscription id).
    path("api/v1/platform/", include("apps.billing.urls_platform")),
    # TD-6: provider webhooks land on the public schema and resolve the tenant
    # by <center_slug> in the path, then verify the provider signature.
    path("api/v1/webhooks/", include("apps.payments.webhook_urls")),
]

# Serve /static/ (platform admin CSS/JS) from the app when DEBUG — see config/urls.py.
from django.conf import settings  # noqa: E402

if settings.DEBUG:
    from django.contrib.staticfiles.urls import staticfiles_urlpatterns

    urlpatterns += staticfiles_urlpatterns()

# Backend API: Django's own error responses (unmatched URL, uncaught 500, CSRF
# 403, suspicious-operation 400) return the flat {"success": false, "code", "message"}
# JSON envelope — identical to what the layered views and DRF handler emit.
handler400 = "core.middleware.json_400"
handler403 = "core.middleware.json_403"
handler404 = "core.middleware.json_404"
handler500 = "core.middleware.json_500"
