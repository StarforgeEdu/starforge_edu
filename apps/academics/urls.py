from django.urls import path

from apps.academics.views.v1 import academics_views as views

urlpatterns = [
    # Subjects
    path("subjects/", views.subjects_collection_view, name="subject-list"),
    path("subjects/<int:pk>/", views.subject_detail_view, name="subject-detail"),
    # Exam types (per-Center configurable exam kinds)
    path("exam-types/", views.exam_types_collection_view, name="exam-type-list"),
    path("exam-types/<int:pk>/", views.exam_type_detail_view, name="exam-type-detail"),
    # Exams (+ per-student results / CSV import / publish actions)
    path("exams/", views.exams_collection_view, name="exam-list"),
    path("exams/<int:pk>/results/import-csv/", views.exam_import_csv_view, name="exam-import-csv"),
    path("exams/<int:pk>/results/", views.exam_results_view, name="exam-results"),
    path("exams/<int:pk>/publish/", views.exam_publish_view, name="exam-publish"),
    path("exams/<int:pk>/", views.exam_detail_view, name="exam-detail"),
    # Grades (read-only computed) + recompute — recompute BEFORE the <pk> route
    path("grades/recompute/", views.grade_recompute_view, name="grade-recompute"),
    path("grades/", views.grades_collection_view, name="grade-list"),
    path("grades/<int:pk>/", views.grade_detail_view, name="grade-detail"),
    # Transcripts (async PDF)
    path("transcripts/", views.transcripts_collection_view, name="transcript-list"),
    path("transcripts/<int:pk>/", views.transcript_detail_view, name="transcript-detail"),
    # Staff-only aggregates
    path("honor-roll/", views.honor_roll_view, name="honor-roll"),
    path("warnings/", views.warnings_view, name="warnings"),
]
