"""Tenant model: Center + Domain.

Center is the tenant — each row owns a Postgres schema. Domain maps
hostnames (subdomains) to Centers. These models live ONLY in the public
schema (apps.tenancy is in SHARED_APPS only).

DO NOT add tenant-scoped fields here (e.g. branch references). Those
belong in apps.org under the tenant schemas.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _
from django_tenants.models import DomainMixin, TenantMixin


class Center(TenantMixin):
    """A customer education center. One row = one Postgres schema."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)

    # Contact + ops metadata (platform-level, not tenant-scoped).
    contact_name = models.CharField(max_length=200, blank=True)
    contact_phone = models.CharField(max_length=32, blank=True)
    contact_email = models.EmailField(blank=True)

    is_active = models.BooleanField(default=True)
    on_trial = models.BooleanField(default=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # django-tenants will run TENANT_APPS migrations the first time this
    # row is saved when auto_create_schema is True.
    auto_create_schema = True
    auto_drop_schema = False  # never auto-drop in prod; explicit deletion only

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.name} ({self.schema_name})"


class Domain(DomainMixin):
    """Hostname → Center mapping. One Center can have multiple Domains."""

    class Meta:
        constraints = [
            # DB-level backstop for the one-primary invariant that
            # services.set_primary_domain maintains under row locks.
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_primary=True),
                name="one_primary_domain_per_tenant",
            ),
        ]


class PlatformEvent(models.Model):
    """Append-only platform-control-center audit trail (public schema, TD-10).

    Written on every platform-staff mutation that the tenant-schema AuditLog
    cannot capture (lifecycle suspend/activate/extend-trial, subscription
    change, impersonation mint). There is NO update/delete API — the model is
    immutable once written, mirroring the tenant-side append-only AuditLog.

    Lives ONLY in the public schema (apps.tenancy is in SHARED_APPS); `actor`
    is a public-schema platform-staff User (TD-3), `center` the affected tenant.
    """

    class Event(models.TextChoices):
        CENTER_SUSPENDED = "center.suspended", _("Center suspended")
        CENTER_ACTIVATED = "center.activated", _("Center activated")
        CENTER_TRIAL_EXTENDED = "center.trial_extended", _("Center trial extended")
        CENTER_CREATED = "center.created", _("Center created")
        SUBSCRIPTION_CHANGED = "subscription.changed", _("Subscription changed")
        IMPERSONATION_MINTED = "impersonation.minted", _("Impersonation token minted")

    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_events",
    )
    center = models.ForeignKey(
        Center,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_events",
    )
    event = models.CharField(max_length=64, choices=Event.choices, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("center", "created_at"), name="pe_center_created_idx"),
            models.Index(fields=("event", "created_at"), name="pe_event_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.event}@{self.center_id}"
