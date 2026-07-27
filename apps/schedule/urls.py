from django.urls import path

from apps.schedule.views.v1 import schedule_views as views

urlpatterns = [
    # Terms
    path("terms/", views.terms_collection_view, name="term-list"),
    path("terms/<int:pk>/", views.term_detail_view, name="term-detail"),
    # Time slots (branch object-scoped on create + detail)
    path("timeslots/", views.time_slots_collection_view, name="timeslot-list"),
    path("timeslots/<int:pk>/", views.time_slot_detail_view, name="timeslot-detail"),
    # Lesson types
    path("lesson-types/", views.lesson_types_collection_view, name="lesson-type-list"),
    path("lesson-types/<int:pk>/", views.lesson_type_detail_view, name="lesson-type-detail"),
    # Recurrence rules (+ bulk reschedule action)
    path("rules/", views.rules_collection_view, name="rule-list"),
    path("rules/<int:pk>/", views.rule_detail_view, name="rule-detail"),
    path("rules/<int:pk>/bulk-reschedule/", views.rule_bulk_reschedule_view, name="rule-bulk-reschedule"),
    # Lessons (read-only scoped feed + cancel/move actions)
    path("lessons/", views.lessons_collection_view, name="lesson-list"),
    path("lessons/<int:pk>/", views.lesson_detail_view, name="lesson-detail"),
    path("lessons/<int:pk>/cancel/", views.lesson_cancel_view, name="lesson-cancel"),
    path("lessons/<int:pk>/move/", views.lesson_move_view, name="lesson-move"),
    # iCal feed
    path("ical-url/", views.ical_url_view, name="ical-url"),
    path("ical/<str:token>/", views.ical_feed_view, name="ical-feed"),
]
