"""Root conftest — two-tenant fixture set. Spec: agents/TESTING.md §2."""

import factory.random
import pytest
from django_tenants.utils import schema_context
from rest_framework.test import APIClient

TENANTS = {"tenant_a": "a.localhost", "tenant_b": "b.localhost"}


def _ensure_tenants() -> None:
    from apps.org.models import CenterSettings
    from apps.tenancy.models import Center
    from apps.tenancy.services import provision_center

    for slug, host in TENANTS.items():
        if not Center.objects.filter(schema_name=slug).exists():
            # Cheap if the schema already exists (django-tenants skips creation
            # via check_if_exists) — only the rows are restored.
            provision_center(name=slug.replace("_", " ").title(), slug=slug, primary_domain=host)
        else:
            # A `transaction=True` test flushes tenant tables. If the public Center
            # row survives (so provision_center is skipped above) the tenant's
            # CenterSettings singleton can still be gone — restore it so an
            # order-dependent test that asserts a fully-provisioned tenant never sees
            # a half-healed one (get_or_create pk=1; a no-op when already present).
            with schema_context(slug):
                CenterSettings.load()


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    """Provision tenant_a + tenant_b once per session (the slow part)."""
    with django_db_blocker.unblock():
        _ensure_tenants()


@pytest.fixture(scope="session", autouse=True)
def _deterministic_seeds():
    factory.random.reseed_random("starforge")  # CI == local, always


@pytest.fixture(autouse=True)
def _clear_cache():
    """LocMemCache is a process-global singleton the DB rollback does NOT reset.
    Clear it around every test so throttle buckets, the OTP per-IP cap, and the
    CenterSettings cache can't bleed across tests (order-dependent 429s, etc.)."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_schema_to_public():
    """A client request through TenantMainMiddleware leaves connection.schema_name
    on that tenant — django-tenants does not reset it at request end. Without this,
    a test that ran a tenant-host request poisons the NEXT test's public-schema
    work (provisioning guards on 'must be public', platform API, archive), causing
    order-dependent failures. Reset to public before every test."""
    from django.db import connection

    connection.set_schema_to_public()


def _get_tenant(slug):
    from apps.tenancy.models import Center

    _ensure_tenants()  # self-heal after a transaction=True flush
    return Center.objects.get(schema_name=slug)


@pytest.fixture
def tenant_a(db):
    return _get_tenant("tenant_a")


@pytest.fixture
def tenant_b(db):
    return _get_tenant("tenant_b")


@pytest.fixture
def api_client():
    # NOTE: bare APIClient() uses host "testserver", which django-tenants 404s
    # unless the public_tenant fixture has mapped it to the public schema.
    return APIClient()


@pytest.fixture
def public_tenant(db):
    """Map host "testserver" to the public schema so tests can hit the platform
    surface (apex /admin/, /api/v1/platform/)."""
    from django_tenants.utils import get_public_schema_name

    from apps.tenancy.models import Center, Domain

    center, _ = Center.objects.get_or_create(
        schema_name=get_public_schema_name(),
        defaults={"name": "Platform", "slug": "platform"},
    )
    Domain.objects.get_or_create(domain="testserver", tenant=center, defaults={"is_primary": True})
    return center


@pytest.fixture
def client_for():
    """client_for(tenant) → APIClient with the tenant's host pre-bound.
    django-tenants resolves schema from the Host header — never call a tenant
    endpoint without it."""

    def _make(tenant):
        host = tenant.domains.get(is_primary=True).domain
        return APIClient(HTTP_HOST=host)

    return _make


@pytest.fixture
def user_in():
    """user_in(tenant, roles=[...]) → User in that tenant's schema with
    RoleMemberships (creates a Branch if none supplied)."""

    def _make(tenant, *, roles=(), branch=None, **kwargs):
        from apps.org.tests.factories import BranchFactory
        from apps.users.models import RoleMembership
        from apps.users.tests.factories import UserFactory

        with schema_context(tenant.schema_name):
            user = UserFactory(**kwargs)
            if roles:
                branch = branch or BranchFactory()
                for role in roles:
                    RoleMembership.objects.create(user=user, branch=branch, role=role)
                # Granting a role bumps token_version (F-expr) in the DB; refresh
                # so the token minted next carries the current tv (else token_stale).
                user.refresh_from_db()
        return user

    return _make


@pytest.fixture
def as_user(client_for):
    """as_user(tenant, user) → authed APIClient. Mints a REAL access token via
    apps.auth.services.issue_token inside the tenant schema, so the TD-1
    schema/tv claims are exercised for free."""

    def _make(tenant, user):
        from apps.auth.services import issue_token

        with schema_context(tenant.schema_name):
            access = issue_token(user)["access"]
        client = client_for(tenant)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client

    return _make


@pytest.fixture
def as_role(tenant_a, user_in, as_user):
    """as_role("teacher") → (client, user) on tenant_a. The matrix workhorse."""

    def _make(role, tenant=None):
        tenant = tenant or tenant_a
        user = user_in(tenant, roles=[role])
        return as_user(tenant, user), user

    return _make


@pytest.fixture
def sms_outbox():
    from infrastructure.sms.eskiz_client import MockEskizClient

    MockEskizClient.outbox.clear()
    return MockEskizClient.outbox


@pytest.fixture
def s3_stub(monkeypatch):
    """In-memory S3 (TESTING.md / D2-F-4): patches every content S3 helper onto a
    recording stub. Reused by storage, payment-receipt, and report tests."""
    from tests.storage_stub import InMemoryS3

    return InMemoryS3().install(monkeypatch)
