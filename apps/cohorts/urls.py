"""Cohort routes — plain function views (off DRF). Mounted at /api/v1/cohorts/."""

from __future__ import annotations

from django.urls import path

from apps.cohorts.views.v1.cohort_views import (
    cohort_detail_view,
    cohort_enroll_view,
    cohort_members_view,
    cohort_move_student_view,
    cohort_remove_student_view,
    cohort_teacher_detail_view,
    cohort_teachers_view,
    cohort_unarchive_view,
    cohorts_collection_view,
)

urlpatterns = [
    path("", cohorts_collection_view, name="cohorts-collection"),
    path("<int:pk>/", cohort_detail_view, name="cohorts-detail"),
    path("<int:pk>/enroll/", cohort_enroll_view, name="cohorts-enroll"),
    path("<int:pk>/move-student/", cohort_move_student_view, name="cohorts-move-student"),
    path("<int:pk>/remove-student/", cohort_remove_student_view, name="cohorts-remove-student"),
    path("<int:pk>/members/", cohort_members_view, name="cohorts-members"),
    path("<int:pk>/teachers/", cohort_teachers_view, name="cohorts-teachers"),
    path(
        "<int:pk>/teachers/<int:teacher_id>/",
        cohort_teacher_detail_view,
        name="cohorts-teacher-detail",
    ),
    path("<int:pk>/unarchive/", cohort_unarchive_view, name="cohorts-unarchive"),
]
