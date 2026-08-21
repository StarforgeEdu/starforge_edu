from django.urls import path

from apps.compliance.views.v1.compliance_views import (
    penalties_collection_view,
    penalty_detail_view,
    penalty_staff_view,
    penalty_waive_view,
    rule_acknowledge_view,
    rule_detail_view,
    rule_mine_view,
    rule_pending_view,
    rules_collection_view,
)

urlpatterns = [
    # Rules — specific action routes before the bare "<pk>/".
    path("rules/", rules_collection_view, name="rule-list"),
    path("rules/mine/", rule_mine_view, name="rule-mine"),
    path("rules/pending/", rule_pending_view, name="rule-pending"),
    path("rules/<int:pk>/", rule_detail_view, name="rule-detail"),
    path("rules/<int:pk>/acknowledge/", rule_acknowledge_view, name="rule-acknowledge"),
    # Penalties — "staff/" before the bare "<pk>/".
    path("penalties/", penalties_collection_view, name="penalty-list"),
    path("penalties/staff/", penalty_staff_view, name="penalty-staff"),
    path("penalties/<int:pk>/", penalty_detail_view, name="penalty-detail"),
    path("penalties/<int:pk>/waive/", penalty_waive_view, name="penalty-waive"),
]
