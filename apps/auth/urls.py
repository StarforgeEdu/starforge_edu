from django.urls import path

from apps.auth.views.v1.auth_views import (
    logout_all_view,
    logout_view,
    password_change_view,
    password_reset_confirm_view,
    password_reset_request_view,
    role_login_view,
    session_bootstrap_view,
)

urlpatterns = [
    path("session/", session_bootstrap_view, name="session-bootstrap"),
    path("role-login/", role_login_view, name="role-login"),
    path("logout/", logout_view, name="logout"),
    path("logout-all/", logout_all_view, name="logout-all"),
    path("password/change/", password_change_view, name="password-change"),
    path("password/reset/request/", password_reset_request_view, name="password-reset-request"),
    path("password/reset/confirm/", password_reset_confirm_view, name="password-reset-confirm"),
]
