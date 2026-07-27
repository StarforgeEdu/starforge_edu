from django.urls import path

from apps.intelligence.views.v1 import intelligence_views as views

urlpatterns = [
    path("risk/", views.risk_list_view, name="risk-list"),
    path("risk/<int:student_id>/", views.risk_detail_view, name="risk-detail"),
    path("branches/", views.branch_ranking_view, name="branch-ranking"),
    path("families/", views.family_health_view, name="family-health"),
    path("journey/<int:student_id>/", views.student_journey_view, name="student-journey"),
    path("teachers/", views.teacher_engagement_view, name="teacher-engagement"),
    path("rules/", views.rules_view, name="risk-rules"),
]
