"""Public-schema webhook URLConf (D3-B-5, TD-6).

Included by ``config/urls_public.py`` at ``api/v1/webhooks/`` so the final shape
is ``POST /api/v1/webhooks/<provider>/<center_slug>/``. Lives on the PUBLIC
URLConf, not the tenant one — providers push to the apex host; the view resolves
the tenant from ``center_slug`` and enters ``schema_context`` (see webhook_views).
"""

from __future__ import annotations

from django.urls import path

from apps.payments.webhook_views import click_webhook_view, payme_webhook_view, uzum_webhook_view

urlpatterns = [
    path("click/<slug:center_slug>/", click_webhook_view, name="webhook-click"),
    path("payme/<slug:center_slug>/", payme_webhook_view, name="webhook-payme"),
    path("uzum/<slug:center_slug>/", uzum_webhook_view, name="webhook-uzum"),
]
