"""Forms routes — plain function views (off DRF). Mounted at /api/v1/forms/."""

from __future__ import annotations

from django.urls import path

from apps.forms.views.v1.form_views import (
    form_add_field_view,
    form_analyze_view,
    form_close_view,
    form_detail_view,
    form_publish_view,
    form_responses_view,
    form_submit_view,
    form_summary_view,
    forms_collection_view,
)

urlpatterns = [
    path("", forms_collection_view, name="forms-collection"),
    path("<int:pk>/", form_detail_view, name="forms-detail"),
    path("<int:pk>/fields/", form_add_field_view, name="forms-add-field"),
    path("<int:pk>/publish/", form_publish_view, name="forms-publish"),
    path("<int:pk>/close/", form_close_view, name="forms-close"),
    path("<int:pk>/submit/", form_submit_view, name="forms-submit"),
    path("<int:pk>/responses/", form_responses_view, name="forms-responses"),
    path("<int:pk>/summary/", form_summary_view, name="forms-summary"),
    path("<int:pk>/analyze/", form_analyze_view, name="forms-analyze"),
]
