"""Same-origin browser session transport: HttpOnly cookie + mandatory CSRF."""

import pytest
from django.conf import settings
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

from core.permissions import Role

pytestmark = pytest.mark.django_db

CSRF_URL = "/api/v1/auth/session/"
ROLE_LOGIN_URL = "/api/v1/auth/role-login/"
LOGOUT_URL = "/api/v1/auth/logout/"
LOGOUT_ALL_URL = "/api/v1/auth/logout-all/"
ME_URL = "/api/v1/users/me/"
PASSWORD = "Cookie-Only-Test-42"


def _browser_client(tenant) -> APIClient:
    return APIClient(
        enforce_csrf_checks=True,
        HTTP_HOST=tenant.domains.get(is_primary=True).domain,
    )


def _director(tenant):
    from apps.org.services import create_staff_account
    from apps.org.tests.factories import BranchFactory
    from apps.users.services import set_role_account_password

    with schema_context(tenant.schema_name):
        branch = BranchFactory()
        director = create_staff_account(
            branch=branch,
            role=Role.DIRECTOR,
            username="browser.director",
            first_name="Browser",
            last_name="Director",
        )
        set_role_account_password(director, PASSWORD, must_change=False)
        return director


def test_cookie_login_requires_csrf_and_never_exposes_the_session_key(tenant_a):
    director = _director(tenant_a)
    client = _browser_client(tenant_a)
    bootstrap = client.get(CSRF_URL)
    assert bootstrap.status_code == 200
    csrf_token = bootstrap.json()["data"]["csrf_token"]
    assert csrf_token
    assert settings.CSRF_COOKIE_NAME in client.cookies

    missing_csrf = client.post(
        ROLE_LOGIN_URL,
        {"username": director.username, "password": PASSWORD},
        format="json",
        HTTP_X_SESSION_TRANSPORT="cookie",
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "csrf_failed"

    response = client.post(
        ROLE_LOGIN_URL,
        {"username": f"  {director.username}  ", "password": PASSWORD},
        format="json",
        HTTP_X_SESSION_TRANSPORT="cookie",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert response.status_code == 200, response.content
    assert "access" not in response.json()["data"]
    assert response["Cache-Control"] == "no-store"

    session_cookie = response.cookies[settings.API_SESSION_COOKIE_NAME]
    assert session_cookie.value
    assert session_cookie["httponly"]
    assert session_cookie["samesite"] == "Lax"
    assert bool(session_cookie["secure"]) is settings.API_SESSION_COOKIE_SECURE
    assert session_cookie["path"] == "/"


def test_cookie_is_shared_across_tabs_and_unsafe_requests_require_csrf(tenant_a):
    director = _director(tenant_a)
    first_tab = _browser_client(tenant_a)
    bootstrap = first_tab.get(CSRF_URL)
    csrf_token = bootstrap.json()["data"]["csrf_token"]
    login = first_tab.post(
        ROLE_LOGIN_URL,
        {"username": director.username, "password": PASSWORD},
        format="json",
        HTTP_X_SESSION_TRANSPORT="cookie",
        HTTP_X_CSRFTOKEN=csrf_token,
    )
    assert login.status_code == 200, login.content

    # A separate browser tab has no JavaScript token to copy. Browsers share the
    # host-only cookie automatically; mirror that cookie jar behavior here.
    second_tab = _browser_client(tenant_a)
    second_tab.cookies[settings.API_SESSION_COOKIE_NAME] = first_tab.cookies[
        settings.API_SESSION_COOKIE_NAME
    ].value
    me = second_tab.get(ME_URL)
    assert me.status_code == 200, me.content
    assert me.json()["data"]["username"] == director.username

    rejected = first_tab.post(LOGOUT_URL, {}, format="json")
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "csrf_failed"

    logout = first_tab.post(LOGOUT_URL, {}, format="json", HTTP_X_CSRFTOKEN=csrf_token)
    assert logout.status_code == 200
    assert logout.json()["success"] is True
    assert logout.cookies[settings.API_SESSION_COOKIE_NAME]["max-age"] == 0
    assert second_tab.get(ME_URL).status_code == 401


def test_bearer_clients_remain_compatible_without_csrf(tenant_a, user_in):
    from apps.auth.services import issue_token

    user = user_in(tenant_a, roles=[Role.DIRECTOR])
    with schema_context(tenant_a.schema_name):
        access = issue_token(user)["access"]
    client = _browser_client(tenant_a)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    response = client.post(LOGOUT_URL, {}, format="json")
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert settings.API_SESSION_COOKIE_NAME not in response.cookies


@pytest.mark.parametrize("url", [LOGOUT_URL, LOGOUT_ALL_URL])
def test_unauthenticated_cross_site_logout_cannot_expire_browser_cookie(tenant_a, url):
    client = _browser_client(tenant_a)

    response = client.post(
        url,
        {},
        format="json",
        HTTP_ORIGIN="https://attacker.invalid",
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert settings.API_SESSION_COOKIE_NAME not in response.cookies
