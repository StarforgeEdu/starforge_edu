"""Tenant lifecycle tests (D1-LB-6/7/8) — fixtures per agents/TESTING.md §2."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.tenancy.models import Domain
from apps.tenancy.services import add_domain, archive_center, set_primary_domain
from core.exceptions import NotFoundException, ValidationException

pytestmark = pytest.mark.django_db


def test_inactive_center_503(tenant_a, client_for):
    tenant_a.is_active = False
    tenant_a.save(update_fields=["is_active"])
    # Anonymous is fine — InactiveTenantMiddleware runs before authentication.
    resp = client_for(tenant_a).get("/api/v1/org/settings/")
    assert resp.status_code == 503
    assert resp.json()["code"] == "center_inactive"


def test_trial_expiry_flips_is_active(tenant_a):
    from celery_tasks.tenancy_tasks import deactivate_expired_trials

    tenant_a.on_trial = True
    tenant_a.trial_ends_at = timezone.now() - timedelta(hours=1)
    tenant_a.save(update_fields=["on_trial", "trial_ends_at"])

    assert deactivate_expired_trials() == 1
    tenant_a.refresh_from_db()
    assert tenant_a.is_active is False
    # Idempotent by filter — a second run flips nothing (D1-LB-7).
    assert deactivate_expired_trials() == 0


def test_archive_renames_schema(tenant_b):
    archived = archive_center(tenant_b)

    assert archived.archived_at is not None
    assert archived.is_active is False
    expected = f"_archived_tenant_b_{archived.archived_at:%Y%m%d}"
    assert archived.schema_name == expected

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name IN (%s, %s)",
            ["tenant_b", expected],
        )
        names = {row[0] for row in cursor.fetchall()}
    assert names == {expected}  # renamed schema exists, old name is gone

    with pytest.raises(ValidationException) as exc:
        archive_center(archived)
    assert exc.value.code == "already_archived"


def test_set_primary_domain_atomic(tenant_a, tenant_b):
    old_primary = tenant_a.domains.get(is_primary=True)
    new = add_domain(tenant_a, domain="alt-a.localhost")
    assert new.is_primary is False

    promoted = set_primary_domain(tenant_a, new.pk)
    assert promoted.is_primary is True
    primaries = Domain.objects.filter(tenant=tenant_a, is_primary=True)
    assert primaries.count() == 1
    assert primaries.get().pk == new.pk
    old_primary.refresh_from_db()
    assert old_primary.is_primary is False  # demoted in the same transaction

    foreign = tenant_b.domains.get(is_primary=True)
    with pytest.raises(NotFoundException):
        set_primary_domain(tenant_a, foreign.pk)


def test_add_duplicate_domain_rejected(tenant_a, tenant_b):
    taken = tenant_b.domains.get(is_primary=True).domain
    with pytest.raises(ValidationException) as exc:
        add_domain(tenant_a, domain=taken)
    assert exc.value.code == "domain_taken"


def test_one_primary_domain_db_constraint(tenant_a):
    """The partial unique index rejects a second primary even when service-level
    locking is bypassed (queryset.update skips DomainMixin.save)."""
    extra = add_domain(tenant_a, domain="extra-a.localhost")
    with pytest.raises(IntegrityError), transaction.atomic():
        Domain.objects.filter(pk=extra.pk).update(is_primary=True)
