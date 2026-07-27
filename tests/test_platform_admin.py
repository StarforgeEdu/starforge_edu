"""Public-schema platform admin + platform API (D1-LB-1, TD-3).

The platform surface (apex /admin/ and /api/v1/platform/) is served by
PUBLIC_SCHEMA_URLCONF, which django-tenants selects only when the request's
hostname resolves to a tenant whose schema_name is "public". The fixture
below creates that standard public-tenant row for the test client's
"testserver" host (no schema is created — "public" already exists).
"""

import pytest
from django.test import Client
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

PASSWORD = "s3cret-Platform!"

# public_tenant fixture lives in the root conftest.py (promoted so any suite
# can hit the platform surface on the "testserver" host).


@pytest.fixture
def platform_admin(public_tenant):
    from apps.users.models import User

    # Public schema is the default connection schema for the test transaction.
    return User.objects.create_superuser(username="padmin", password=PASSWORD)


def test_public_schema_platform_admin_login(platform_admin):
    client = Client()  # host "testserver" → public schema URLConf
    resp = client.post(
        "/admin/login/",
        {"username": "padmin", "password": PASSWORD, "next": "/admin/"},
    )
    assert resp.status_code == 302
    assert resp["Location"] == "/admin/"
    assert resp.wsgi_request.user.is_authenticated


def _staff_client(user):
    # The layered platform views use the custom session authenticator (not DRF
    # force_authenticate) — mint a real public-schema session key.
    from core.session_auth import create_session

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {create_session(user).key}")
    return client


def test_platform_centers_api_staff_200(platform_admin, tenant_a):
    resp = _staff_client(platform_admin).get("/api/v1/platform/centers/")
    assert resp.status_code == 200
    rows = resp.json()["data"]  # layered {success, data, pagination}
    assert tenant_a.slug in {row["slug"] for row in rows}


def test_platform_centers_api_non_staff_403(public_tenant, tenant_a):
    """A public-schema NON-staff user authenticates but is denied (is_staff gate).

    (A tenant-schema user is rejected earlier with 401 — no public Session row —
    covered by the D4-LE-7 apex lockdown tests; here we exercise the is_staff
    branch, so the user must be a real public-schema user.)"""
    from apps.users.models import User

    user = User.objects.create_user(username="plat-plain", password=PASSWORD)  # is_staff=False
    assert _staff_client(user).get("/api/v1/platform/centers/").status_code == 403


def test_set_primary_non_numeric_domain_id_404(platform_admin, tenant_a):
    """Routing regression: <int:domain_id> must reject non-numeric ids at the
    resolver (404) instead of int() exploding into a 500."""
    resp = _staff_client(platform_admin).post(
        f"/api/v1/platform/centers/{tenant_a.pk}/domains/abc/set-primary/"
    )
    assert resp.status_code == 404
