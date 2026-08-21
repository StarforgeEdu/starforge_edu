"""Tenant lifecycle tests (D1-LB-6/7/8) — fixtures per agents/TESTING.md §2."""

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.tenancy.models import Domain, DomainClaim, PlatformEvent
from apps.tenancy.services import (
    activate_center,
    add_domain,
    archive_center,
    set_primary_domain,
    suspend_center,
    verify_domain,
)
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
    assert tenant_a.on_trial is False
    assert PlatformEvent.objects.filter(
        center=tenant_a, event=PlatformEvent.Event.CENTER_TRIAL_EXPIRED
    ).exists()
    # Idempotent by filter — a second run flips nothing (D1-LB-7).
    assert deactivate_expired_trials() == 0


def test_manual_activation_of_expired_trial_is_not_undone_by_beat(tenant_a):
    from celery_tasks.tenancy_tasks import deactivate_expired_trials

    tenant_a.is_active = False
    tenant_a.on_trial = True
    tenant_a.trial_ends_at = timezone.now() - timedelta(hours=1)
    tenant_a.save(update_fields=["is_active", "on_trial", "trial_ends_at"])

    activate_center(tenant_a)
    tenant_a.refresh_from_db()
    assert tenant_a.is_active is True
    assert tenant_a.on_trial is False
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


def test_custom_domain_is_unroutable_until_txt_verification(tenant_a, client_for, monkeypatch):
    claim = add_domain(tenant_a, domain="portal.customer.example", is_primary=True)
    assert isinstance(claim, DomainClaim)
    assert claim.is_verified is False
    assert claim.is_primary is False
    assert claim.pending_primary is True
    assert claim.verification_token
    # This is the rollback-safety invariant: old TenantMainMiddleware reads only
    # Domain, so an unverified hostname is invisible even to an old app image.
    assert not Domain.objects.filter(domain=claim.domain).exists()

    unresolved = client_for(tenant_a)
    unresolved.defaults["HTTP_HOST"] = claim.domain
    assert unresolved.get("/api/v1/org/settings/").status_code == 404

    expected = f"starforge-domain-verification={claim.verification_token}"
    monkeypatch.setattr(
        "apps.tenancy.services._lookup_txt_records",
        lambda name: (expected,) if name == f"_starforge-verification.{claim.domain}" else (),
    )
    verified = verify_domain(tenant_a, claim_id=claim.pk)
    assert isinstance(verified, Domain)
    assert verified.is_primary is True
    claim.refresh_from_db()
    assert claim.is_verified is True
    assert claim.pending_primary is False
    assert claim.domain_record_id == verified.pk
    assert claim.verified_at is not None

    resolved = client_for(tenant_a)
    resolved.defaults["HTTP_HOST"] = claim.domain
    assert resolved.get("/api/v1/org/settings/").status_code != 404


def test_domain_verification_fails_closed_without_exact_txt(tenant_a, monkeypatch):
    claim = add_domain(tenant_a, domain="portal.other.example")
    assert isinstance(claim, DomainClaim)
    monkeypatch.setattr("apps.tenancy.services._lookup_txt_records", lambda _name: ("wrong",))
    with pytest.raises(ValidationException) as exc:
        verify_domain(tenant_a, claim_id=claim.pk)
    assert exc.value.code == "domain_verification_failed"
    claim.refresh_from_db()
    assert claim.is_verified is False
    assert not Domain.objects.filter(domain=claim.domain).exists()


def test_domain_schema_remains_compatible_with_old_application_writes(tenant_a):
    """The claim migration must not add required columns to Domain.

    An old image can still insert its original three-field Domain row while a
    rolling migration is in progress; more importantly, it cannot see pending
    rows because those exist only in DomainClaim.
    """
    row = Domain.objects.create(domain="old-node.localhost", tenant=tenant_a, is_primary=False)
    assert row.pk is not None


def test_archived_center_control_plane_writes_are_rejected(tenant_a):
    tenant_a.archived_at = timezone.now()
    tenant_a.is_active = False
    tenant_a.save(update_fields=["archived_at", "is_active"])

    for mutation in (
        lambda: activate_center(tenant_a),
        lambda: add_domain(tenant_a, domain="archived.customer.example"),
    ):
        with pytest.raises(ValidationException) as exc:
            mutation()
        assert exc.value.code == "center_archived"


def test_suspend_without_subscription_enforces_inactive_gate(tenant_a):
    from apps.billing.models import Subscription

    Subscription.objects.filter(center=tenant_a).delete()
    suspend_center(tenant_a, reason="manual security hold")
    tenant_a.refresh_from_db()
    assert tenant_a.is_active is False
    event = PlatformEvent.objects.filter(
        center=tenant_a,
        event=PlatformEvent.Event.CENTER_SUSPENDED,
    ).latest("created_at")
    assert event.payload["enforcement"] == "inactive_center"
