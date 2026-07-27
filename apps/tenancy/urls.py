"""Platform control-center URLs (public schema, mounted at /api/v1/platform/).

Plain function views (off DRF). Specific literal paths (resolve/, the action
sub-routes) are declared before the ``<int:pk>`` catch-alls.
"""

from django.urls import path

from apps.tenancy.views.v1 import tenancy_views as views

urlpatterns = [
    # TD-19 tenant resolution — public, anon-throttled. Declared first.
    path("resolve/", views.resolve_view, name="platform-resolve"),
    # Center lifecycle (platform staff only).
    path("centers/", views.centers_collection_view, name="center-collection"),
    path("centers/<int:pk>/", views.center_detail_view, name="center-detail"),
    path("centers/<int:pk>/suspend/", views.center_suspend_view, name="center-suspend"),
    path("centers/<int:pk>/activate/", views.center_activate_view, name="center-activate"),
    path("centers/<int:pk>/extend-trial/", views.center_extend_trial_view, name="center-extend-trial"),
    path("centers/<int:pk>/usage/", views.center_usage_view, name="center-usage"),
    path("centers/<int:pk>/impersonate/", views.center_impersonate_view, name="center-impersonate"),
    path("centers/<int:pk>/domains/", views.center_domains_view, name="center-domains"),
    path(
        "centers/<int:pk>/domains/<int:domain_id>/set-primary/",
        views.center_set_primary_domain_view,
        name="center-set-primary",
    ),
]
