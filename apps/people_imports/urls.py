from django.urls import path

from apps.people_imports.views import (
    import_confirm_view,
    import_detail_view,
    import_rows_view,
    imports_collection_view,
)

urlpatterns = [
    path("", imports_collection_view, name="people-imports-list"),
    path("<int:pk>/", import_detail_view, name="people-imports-detail"),
    path("<int:pk>/rows/", import_rows_view, name="people-imports-rows"),
    path("<int:pk>/confirm/", import_confirm_view, name="people-imports-confirm"),
]
