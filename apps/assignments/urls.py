"""Assignment routes — plain function views (off DRF). Mounted at /api/v1/assignments/.

The `submissions/` + `upload-url/` routes are declared BEFORE the assignment catch-all
so `/assignments/submissions/...` is not swallowed by the `{pk}` detail route.
"""

from __future__ import annotations

from django.urls import path

from apps.assignments.views.v1.assignment_views import (
    assignment_close_view,
    assignment_detail_view,
    assignment_publish_view,
    assignment_submissions_view,
    assignment_upload_url_view,
    assignments_collection_view,
    submission_ai_feedback_view,
    submission_detail_view,
    submission_grade_view,
    submission_plagiarism_view,
    submission_return_view,
    submissions_collection_view,
)

urlpatterns = [
    # Submissions (top-level) — declared before the assignment catch-all.
    path("submissions/", submissions_collection_view, name="submissions-collection"),
    path("submissions/<int:pk>/", submission_detail_view, name="submissions-detail"),
    path("submissions/<int:pk>/grade/", submission_grade_view, name="submissions-grade"),
    path("submissions/<int:pk>/return/", submission_return_view, name="submissions-return"),
    path(
        "submissions/<int:pk>/plagiarism/",
        submission_plagiarism_view,
        name="submissions-plagiarism",
    ),
    path(
        "submissions/<int:pk>/request-ai-feedback/",
        submission_ai_feedback_view,
        name="submissions-ai-feedback",
    ),
    # Presigned upload (collection action).
    path("upload-url/", assignment_upload_url_view, name="assignments-upload-url"),
    # Assignments.
    path("", assignments_collection_view, name="assignments-collection"),
    path("<int:pk>/", assignment_detail_view, name="assignments-detail"),
    path("<int:pk>/publish/", assignment_publish_view, name="assignments-publish"),
    path("<int:pk>/close/", assignment_close_view, name="assignments-close"),
    path("<int:pk>/submissions/", assignment_submissions_view, name="assignments-submissions"),
]
