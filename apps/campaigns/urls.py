"""Campaign routes — plain function views (off DRF). Mounted at /api/v1/campaigns/.

The do-not-contact + template routes are declared BEFORE the campaign catch-all so
they are never shadowed by the campaign detail route.
"""

from __future__ import annotations

from django.urls import path

from apps.campaigns.views.v1.campaign_views import (
    campaign_detail_view,
    campaign_recipients_view,
    campaign_send_view,
    campaigns_collection_view,
    dnc_collection_view,
    dnc_detail_view,
    template_detail_view,
    template_generate_view,
    templates_collection_view,
)

urlpatterns = [
    # Do-not-contact list.
    path("do-not-contact/", dnc_collection_view, name="do-not-contact-collection"),
    path("do-not-contact/<int:pk>/", dnc_detail_view, name="do-not-contact-detail"),
    # Message templates.
    path("templates/", templates_collection_view, name="message-templates-collection"),
    path("templates/<int:pk>/", template_detail_view, name="message-templates-detail"),
    path("templates/<int:pk>/generate/", template_generate_view, name="message-templates-generate"),
    # Campaigns (catch-all last).
    path("", campaigns_collection_view, name="campaigns-collection"),
    path("<int:pk>/", campaign_detail_view, name="campaigns-detail"),
    path("<int:pk>/send/", campaign_send_view, name="campaigns-send"),
    path("<int:pk>/recipients/", campaign_recipients_view, name="campaigns-recipients"),
]
