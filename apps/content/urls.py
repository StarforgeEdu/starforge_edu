from django.urls import path

from apps.content.views.v1 import content_views as views

urlpatterns = [
    # Signed-URL upload entry point
    path("upload-url/", views.content_upload_url_view, name="content-upload-url"),
    # Libraries
    path("libraries/", views.libraries_collection_view, name="content-library-list"),
    path("libraries/<int:pk>/", views.library_detail_view, name="content-library-detail"),
    # Courses
    path("courses/", views.courses_collection_view, name="content-course-list"),
    path("courses/<int:pk>/", views.course_detail_view, name="content-course-detail"),
    # Modules
    path("modules/", views.modules_collection_view, name="content-module-list"),
    path("modules/<int:pk>/", views.module_detail_view, name="content-module-detail"),
    # Content lessons
    path("lessons/", views.lessons_collection_view, name="content-lesson-list"),
    path("lessons/<int:pk>/", views.lesson_detail_view, name="content-lesson-detail"),
    # Folders
    path("folders/", views.folders_collection_view, name="content-folder-list"),
    path("folders/<int:pk>/", views.folder_detail_view, name="content-folder-detail"),
    # Lesson files (+ actions) — action routes precede the generic detail
    path("files/", views.files_collection_view, name="content-file-list"),
    path("files/<int:pk>/confirm/", views.file_confirm_view, name="content-file-confirm"),
    path("files/<int:pk>/download-url/", views.file_download_url_view, name="content-file-download-url"),
    path("files/<int:pk>/track-view/", views.file_track_view_view, name="content-file-track-view"),
    path("files/<int:pk>/new-version/", views.file_new_version_view, name="content-file-new-version"),
    path(
        "files/<int:pk>/approve-teacher/",
        views.file_approve_teacher_view,
        name="content-file-approve-teacher",
    ),
    path(
        "files/<int:pk>/approve-manager/",
        views.file_approve_manager_view,
        name="content-file-approve-manager",
    ),
    path("files/<int:pk>/", views.file_detail_view, name="content-file-detail"),
    # Library materials (+ actions)
    path("materials/", views.materials_collection_view, name="content-material-list"),
    path("materials/<int:pk>/generate/", views.material_generate_view, name="content-material-generate"),
    path("materials/<int:pk>/publish/", views.material_publish_view, name="content-material-publish"),
    path("materials/<int:pk>/", views.material_detail_view, name="content-material-detail"),
]
