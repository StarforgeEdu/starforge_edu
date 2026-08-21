from django.urls import path

from apps.ai.views.v1 import ai_views as views

urlpatterns = [
    path("budget/", views.budget_view, name="ai-budget"),
    path("exam-generation/", views.exam_generation_view, name="ai-exam-generation"),
    path("usage-report/", views.usage_report_view, name="ai-usage-report"),
    path("requests/", views.ai_requests_collection_view, name="ai-request-list"),
    path("requests/<int:pk>/", views.ai_request_detail_view, name="ai-request-detail"),
]
