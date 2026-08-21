"""Reward routes — plain function views (off DRF). Mounted at /api/v1/rewards/."""

from __future__ import annotations

from django.urls import path

from apps.rewards.views.v1.reward_views import (
    reward_grant_detail_view,
    reward_grants_collection_view,
    reward_grants_mine_view,
    reward_type_detail_view,
    reward_types_collection_view,
)

urlpatterns = [
    path("types/", reward_types_collection_view, name="reward-types-collection"),
    path("types/<int:pk>/", reward_type_detail_view, name="reward-types-detail"),
    path("grants/", reward_grants_collection_view, name="reward-grants-collection"),
    path("grants/mine/", reward_grants_mine_view, name="reward-grants-mine"),
    path("grants/<int:pk>/", reward_grant_detail_view, name="reward-grants-detail"),
]
