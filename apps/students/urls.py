"""Student routes — plain function views (off DRF). Mounted at /api/v1/students/.

The self-service (me/…) and collection-action prefixes are listed before the
parent catch-all; detail routes use ``<int:pk>`` so they never capture a literal
segment like "import" or "birthdays".
"""

from __future__ import annotations

from django.urls import path

from apps.students.views.v1.student_views import (
    enrollment_reason_detail_view,
    enrollment_reasons_collection_view,
    student_block_view,
    student_credentials_view,
    student_dashboard_view,
    student_detail_view,
    student_events_view,
    student_report_view,
    student_transition_view,
    student_unblock_view,
    students_birthdays_view,
    students_collection_view,
    students_comparison_view,
    students_import_view,
    students_stats_view,
)

urlpatterns = [
    # Self-service (F4-1 / F15-1) — before the router so "me" isn't read as a pk.
    path("me/dashboard/", student_dashboard_view, name="student-dashboard"),
    path("me/report/", student_report_view, name="student-report"),
    # Collection actions.
    path("import/", students_import_view, name="students-import"),
    path("birthdays/", students_birthdays_view, name="students-birthdays"),
    path("stats/", students_stats_view, name="students-stats"),
    path("comparison/", students_comparison_view, name="students-comparison"),
    # Enrollment reasons (per-Center configurable) — literal segment, before <int:pk>.
    path("enrollment-reasons/", enrollment_reasons_collection_view, name="enrollment-reason-list"),
    path("enrollment-reasons/<int:pk>/", enrollment_reason_detail_view, name="enrollment-reason-detail"),
    # Collection + detail.
    path("", students_collection_view, name="students-collection"),
    path("<int:pk>/", student_detail_view, name="students-detail"),
    path("<int:pk>/transition/", student_transition_view, name="students-transition"),
    path("<int:pk>/block/", student_block_view, name="students-block"),
    path("<int:pk>/unblock/", student_unblock_view, name="students-unblock"),
    path("<int:pk>/events/", student_events_view, name="students-events"),
    path("<int:pk>/credentials/", student_credentials_view, name="students-credentials"),
]
