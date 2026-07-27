"""A-2 — dynamic, center-configurable permissions (server-enforced live).

The static `core.permissions.ROLE_PERMISSION_MATRIX` ships sensible defaults; a
center tailors them with `RolePermissionOverride` rows that GRANT or REVOKE a
specific permission code for a role. The resolver in `core.permissions`
(`has_permission_code` / `role_effective_permissions`) merges them over the
defaults on every request (read once per request, no cross-request cache), so
changes take effect immediately and centrally — no redeploy, no per-view edits.

Anti-fraud invariant: the master wildcard `*:*` cannot be overridden (validated
in the service), so a center can never revoke the director's authority nor
escalate a role to full power through this mechanism.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class RolePermissionOverride(models.Model):
    """One grant/revoke of a permission code for a role, scoped to this center
    (tenant schema). Layered over the static matrix by the resolver."""

    class Effect(models.TextChoices):
        GRANT = "grant", _("Grant")
        REVOKE = "revoke", _("Revoke")

    role = models.CharField(max_length=32, db_index=True)
    permission = models.CharField(max_length=64)  # "students:write" / "students:*"
    effect = models.CharField(max_length=6, choices=Effect.choices)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("role", "permission")
        constraints = [
            models.UniqueConstraint(fields=("role", "permission"), name="one_override_per_role_permission"),
            # The master wildcard is never overridable, enforced at the DB level so NO
            # write path (HTTP service, programmatic service, raw ORM) can revoke the director's
            # authority or escalate a role to everything. Defense in depth — the
            # service also 400s it for a friendly message.
            models.CheckConstraint(condition=~models.Q(permission="*:*"), name="no_master_wildcard_override"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.role}:{self.effect}:{self.permission}"
