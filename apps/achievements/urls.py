"""Achievement routes — plain function views (off DRF). Mounted at /api/v1/achievements/."""

from __future__ import annotations

from django.urls import path

from apps.achievements.views.v1.achievement_views import (
    achievement_approve_view,
    achievement_detail_view,
    achievement_grant_view,
    achievement_grants_view,
    achievement_reject_view,
    achievements_collection_view,
    achievements_mine_view,
)

urlpatterns = [
    path("", achievements_collection_view, name="achievements-collection"),
    path("mine/", achievements_mine_view, name="achievements-mine"),
    path("<int:pk>/", achievement_detail_view, name="achievements-detail"),
    path("<int:pk>/approve/", achievement_approve_view, name="achievements-approve"),
    path("<int:pk>/reject/", achievement_reject_view, name="achievements-reject"),
    path("<int:pk>/grant/", achievement_grant_view, name="achievements-grant"),
    path("<int:pk>/grants/", achievement_grants_view, name="achievements-grants"),
]
