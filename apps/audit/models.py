"""Audit trail model (TD-9, D3-D-1).

`AuditLog` is the append-only record of every sensitive mutation and security
event in a tenant schema. Rows are **immutable**: application code only ever
INSERTs (see `apps.audit.services.audit_log` + `apps.audit.receivers`) and the
retention task (`celery_tasks.audit_tasks`) is the only code that DELETEs, by
age. There is no `updated_at` and no update path — the model deliberately omits
both. Production hardening additionally `REVOKE`s UPDATE/DELETE from the app DB
role (runbook line; the grant itself is `[OWNER:O-9]` hosting — see migration
0002 docstring).
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", _("Create")
        UPDATE = "update", _("Update")
        DELETE = "delete", _("Delete")
        LOGIN = "login", _("Login")
        LOGIN_FAILED = "login_failed", _("Login failed")
        LOGOUT = "logout", _("Logout")
        OTP_REQUEST = "otp_request", _("OTP request")
        OTP_VERIFY = "otp_verify", _("OTP verify")
        IMPERSONATE = "impersonate", _("Impersonate")
        EXPORT = "export", _("Export")

    # SET_NULL: deleting the actor must never cascade away the audit history.
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Frozen snapshot of str(actor) at write time — survives actor deletion and
    # username changes, so the trail stays meaningful even after SET_NULL.
    actor_repr = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=64, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("resource_type", "resource_id")),
            models.Index(fields=("actor",)),
        ]

    def __str__(self) -> str:  # pragma: no cover
        target = f"{self.resource_type}#{self.resource_id}" if self.resource_type else "-"
        return f"{self.action} {target} by {self.actor_repr or 'system'}"
