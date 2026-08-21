from django.urls import path

from apps.teachers.views.v1.teacher_views import (
    teacher_credentials_view,
    teacher_dashboard_view,
    teacher_detail_view,
    teacher_payout_policy_view,
    teacher_prepare_salary_view,
    teachers_collection_view,
)

urlpatterns = [
    path("dashboard/", teacher_dashboard_view, name="teacher-dashboard"),
    path("<int:pk>/payout-policy/", teacher_payout_policy_view, name="teacher-payout-policy"),
    path("<int:pk>/prepare-salary/", teacher_prepare_salary_view, name="teacher-prepare-salary"),
    path("<int:pk>/credentials/", teacher_credentials_view, name="teacher-credentials"),
    path("", teachers_collection_view, name="teachers-list"),
    path("<int:pk>/", teacher_detail_view, name="teachers-detail"),
]
