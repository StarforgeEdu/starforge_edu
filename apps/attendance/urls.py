from django.urls import path

from apps.attendance.views.v1.attendance_views import (
    dashboard_view,
    export_view,
    mark_view,
    record_detail_view,
    records_collection_view,
    summary_view,
)

urlpatterns = [
    path("records/", records_collection_view, name="attendance-record-list"),
    path("records/<int:pk>/", record_detail_view, name="attendance-record-detail"),
    path("lessons/<int:lesson_id>/mark/", mark_view, name="attendance-mark"),
    path("summary/", summary_view, name="attendance-summary"),
    path("cohorts/<int:cohort_id>/dashboard/", dashboard_view, name="attendance-dashboard"),
    path("export/", export_view, name="attendance-export"),
]
