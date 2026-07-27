"""Users URLs (mounted at /api/v1/users/). Plain function views (off DRF).

Specific literal paths (me/, devices/) are declared before the ``<int:pk>``
directory routes so they are matched first.
"""

from django.urls import path

from apps.users.views.v1 import users_views as views

urlpatterns = [
    path("me/", views.me_view, name="user-me"),
    path("devices/", views.devices_collection_view, name="device-collection"),
    path("devices/<int:pk>/", views.device_detail_view, name="device-detail"),
    path("", views.users_collection_view, name="user-collection"),
    path("<int:pk>/", views.user_detail_view, name="user-detail"),
]
