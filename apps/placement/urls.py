"""Placement URLs (mounted at /api/v1/placement/). Plain function views (off DRF).

Action sub-routes are declared before the ``<int:pk>`` detail routes.
"""

from django.urls import path

from apps.placement.views.v1 import placement_views as views

urlpatterns = [
    # --- tests ---
    path("tests/", views.tests_collection_view, name="placement-test-collection"),
    path(
        "tests/<int:pk>/questions/<int:question_id>/remove/",
        views.test_remove_question_view,
        name="placement-test-remove-question",
    ),
    path("tests/<int:pk>/questions/", views.test_add_question_view, name="placement-test-add-question"),
    path("tests/<int:pk>/generate/", views.test_generate_view, name="placement-test-generate"),
    path("tests/<int:pk>/submit/", views.test_submit_view, name="placement-test-submit"),
    path("tests/<int:pk>/approve/", views.test_approve_view, name="placement-test-approve"),
    path("tests/<int:pk>/reject/", views.test_reject_view, name="placement-test-reject"),
    path("tests/<int:pk>/", views.test_detail_view, name="placement-test-detail"),
    # --- attempts ---
    path("attempts/", views.attempts_collection_view, name="placement-attempt-collection"),
    path("attempts/<int:pk>/submit/", views.attempt_submit_view, name="placement-attempt-submit"),
    path("attempts/<int:pk>/suggestions/", views.attempt_suggestions_view, name="placement-attempt-suggestions"),
    path("attempts/<int:pk>/mark-writing/", views.attempt_mark_writing_view, name="placement-attempt-mark-writing"),
    path(
        "attempts/<int:pk>/mark-writing-manual/",
        views.attempt_mark_writing_manual_view,
        name="placement-attempt-mark-writing-manual",
    ),
    path("attempts/<int:pk>/", views.attempt_detail_view, name="placement-attempt-detail"),
    # --- group proposals ---
    path("proposals/", views.proposals_collection_view, name="placement-proposal-collection"),
    path("proposals/<int:pk>/accept/", views.proposal_accept_view, name="placement-proposal-accept"),
    path("proposals/<int:pk>/reject/", views.proposal_reject_view, name="placement-proposal-reject"),
    path("proposals/<int:pk>/", views.proposal_detail_view, name="placement-proposal-detail"),
]
