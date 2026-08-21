"""Task routes — plain function views (off DRF). Mounted at /api/v1/tasks/."""

from __future__ import annotations

from django.urls import path

from apps.tasks.views.v1.task_views import (
    role_grade_detail_view,
    role_grades_collection_view,
    task_assign_view,
    task_auto_assign_view,
    task_detail_view,
    task_transition_view,
    tasks_collection_view,
    tasks_mine_view,
)

urlpatterns = [
    # RoleGrade hierarchy (registered before the Task <int:pk> catch-all).
    path("grades/", role_grades_collection_view, name="role-grades-collection"),
    path("grades/<int:pk>/", role_grade_detail_view, name="role-grades-detail"),
    # Tasks.
    path("", tasks_collection_view, name="tasks-collection"),
    path("mine/", tasks_mine_view, name="tasks-mine"),
    path("auto-assign/", task_auto_assign_view, name="tasks-auto-assign"),
    path("<int:pk>/", task_detail_view, name="tasks-detail"),
    path("<int:pk>/assign/", task_assign_view, name="tasks-assign"),
    path("<int:pk>/transition/", task_transition_view, name="tasks-transition"),
]
