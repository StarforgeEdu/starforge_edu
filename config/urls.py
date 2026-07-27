"""Tenant-schema URLConf — served when a request maps to a tenant subdomain."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularRedocView, SpectacularSwaggerView

import core.schema  # noqa: F401 — registers the OpenAPI auth extension (TD-1)
from core.openapi import openapi_schema_view

api_v1_patterns = [
    path("auth/", include("apps.auth.urls")),
    path("users/", include("apps.users.urls")),
    path("org/", include("apps.org.urls")),
    path("students/", include("apps.students.urls")),
    path("parents/", include("apps.parents.urls")),
    path("teachers/", include("apps.teachers.urls")),
    path("cohorts/", include("apps.cohorts.urls")),
    path("schedule/", include("apps.schedule.urls")),
    path("attendance/", include("apps.attendance.urls")),
    path("academics/", include("apps.academics.urls")),
    path("assignments/", include("apps.assignments.urls")),
    path("content/", include("apps.content.urls")),
    path("printing/", include("apps.printing.urls")),
    path("finance/", include("apps.finance.urls")),
    path("payments/", include("apps.payments.urls")),
    path("notifications/", include("apps.notifications.urls")),
    path("ai/", include("apps.ai.urls")),
    path("audit/", include("apps.audit.urls")),
    path("reports/", include("apps.reports.urls")),
    path("approvals/", include("apps.approvals.urls")),
    path("rulebook/", include("apps.compliance.urls")),
    path("access/", include("apps.access.urls")),
    path("forms/", include("apps.forms.urls")),
    path("tasks/", include("apps.tasks.urls")),
    path("messaging/", include("apps.messaging.urls")),
    path("intelligence/", include("apps.intelligence.urls")),
    path("achievements/", include("apps.achievements.urls")),
    path("rewards/", include("apps.rewards.urls")),
    path("cover/", include("apps.covers.urls")),
    path("loans/", include("apps.loans.urls")),
    path("procurement/", include("apps.procurement.urls")),
    path("campaigns/", include("apps.campaigns.urls")),
    path("sales/", include("apps.sales.urls")),
    path("meetings/", include("apps.meetings.urls")),
    path("placement/", include("apps.placement.urls")),
    path("cards/", include("apps.cards.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include((api_v1_patterns, "v1"))),
    # Full OpenAPI 3.0 schema for the whole tenant API (core.openapi walks every plain view;
    # drf-spectacular alone only saw the lone DRF 'reports' app). Swagger UI + Redoc render it.
    path("api/schema/", openapi_schema_view, name="schema"),
    path("api/schema/swagger-ui/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

# Serve /static/ (admin CSS/JS, schema UI assets) from the app when DEBUG — under gunicorn
# Django does NOT auto-serve static like runserver does, so without this route the admin
# renders unstyled. DEBUG-guarded: a hardened (DEBUG=False) deploy must front static with a
# real static server / CDN instead.
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
