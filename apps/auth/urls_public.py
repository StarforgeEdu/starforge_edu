"""Public-schema authentication for platform/control-center staff."""

from django.urls import path

from apps.auth.views.v1.auth_views import login_view, logout_view, password_change_view

urlpatterns = [
    path("login/", login_view, name="platform-login"),
    path("logout/", logout_view, name="platform-logout"),
    path("password/change/", password_change_view, name="platform-password-change"),
]
