"""Audit trail model (TD-9, D3-D-1).

`AuditLog` is the append-only record of every sensitive mutation and security
event in a public or tenant schema. Rows are **immutable**: application code only ever
INSERTs (see `apps.audit.services.audit_log` + `apps.audit.receivers`) and the
retention task (`celery_tasks.audit_tasks`) is the only code that DELETEs, by
age. There is no `updated_at` and no update path — the model deliberately omits
both. Migration 0004 installs a database trigger that rejects UPDATE/DELETE;
only the retention task's transaction-local maintenance capability can delete
expired rows.
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
        EXPORT_COMPLETE = "export.complete", _("Export completed")
        EXPORT_FAILED = "export.failed", _("Export failed")
        SESSION_REVOKED = "session.revoked", _("Session revoked")
        PRINT_JOB_CREATED = "print.job_created", _("Print job created")
        PRINT_JOB_REJECTED = "print.job_rejected", _("Print job rejected")
        PRINT_JOB_DONE = "print.job_done", _("Print job completed")
        PRINT_JOB_FAILED = "print.job_failed", _("Print job failed")
        PRINT_JOB_RETRY_SCHEDULED = (
            "print.job_retry_scheduled",
            _("Print job retry scheduled"),
        )
        PRINT_JOB_RECONCILIATION_REQUIRED = (
            "print.job_reconciliation_required",
            _("Print job reconciliation required"),
        )
        PRINT_JOB_RECONCILED = "print.job_reconciled", _("Print job reconciled")

    class ScopeStatus(models.TextChoices):
        """How confidently this event is attributed to an organization scope.

        ``SCOPED`` freezes the branch/optional-department boundary known when the
        event was written. ``ORGANIZATION`` is reserved for genuinely
        organization-wide operations. ``UNRESOLVED`` is deliberately fail-closed:
        it covers legacy or ambiguous events and is never exposed to a scoped
        manager merely because the referenced resource now belongs to their branch.
        """

        SCOPED = "scoped", _("Scoped")
        ORGANIZATION = "organization", _("Organization-wide")
        UNRESOLVED = "unresolved", _("Unresolved")

    class Sensitivity(models.TextChoices):
        STANDARD = "standard", _("Standard")
        COMPENSATION = "compensation", _("Compensation")

    class ActorAttributionStatus(models.TextChoices):
        """Quality of the immutable actor identity captured at write time."""

        EXACT = "exact", _("Exact principal")
        SYSTEM = "system", _("System or anonymous")
        UNRESOLVED = "unresolved", _("Unresolved legacy actor")

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
    # The bridge User FK alone does not identify which role-native account made
    # the request. These immutable fields preserve the authenticated principal;
    # ``user`` is used only for public control-center sessions.
    actor_attribution_status = models.CharField(
        max_length=16,
        choices=ActorAttributionStatus.choices,
        default=ActorAttributionStatus.UNRESOLVED,
        db_default=ActorAttributionStatus.UNRESOLVED,
        db_index=True,
    )
    actor_principal_kind = models.CharField(max_length=16, blank=True, db_default="")
    actor_principal_id = models.PositiveBigIntegerField(null=True, blank=True)
    action = models.CharField(max_length=64, choices=Action.choices, db_index=True)
    resource_type = models.CharField(max_length=100, blank=True)
    resource_id = models.CharField(max_length=64, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    # Plain integer snapshots rather than foreign keys: deleting/reorganizing a
    # Branch or Department must not rewrite or erase historical audit ownership.
    scope_status = models.CharField(
        max_length=16,
        choices=ScopeStatus.choices,
        default=ScopeStatus.UNRESOLVED,
        db_index=True,
    )
    scope_branch_id = models.PositiveBigIntegerField(null=True, blank=True)
    scope_department_id = models.PositiveBigIntegerField(null=True, blank=True)
    # Immutable write-time classification. A database INSERT trigger also
    # derives compensation for old application nodes during rolling deploys.
    sensitivity = models.CharField(
        max_length=16,
        choices=Sensitivity.choices,
        default=Sensitivity.STANDARD,
        db_default=Sensitivity.STANDARD,
        db_index=True,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("resource_type", "resource_id")),
            models.Index(fields=("actor",)),
            models.Index(
                fields=(
                    "actor_attribution_status",
                    "actor_principal_kind",
                    "actor_principal_id",
                    "-created_at",
                    "-id",
                ),
                name="audit_actor_principal_time_idx",
            ),
            models.Index(
                fields=("scope_status", "scope_branch_id", "-created_at", "-id"),
                name="audit_scope_branch_time_idx",
            ),
            models.Index(
                fields=(
                    "scope_status",
                    "scope_branch_id",
                    "scope_department_id",
                    "-created_at",
                    "-id",
                ),
                name="audit_scope_dept_time_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        scope_status="scoped",
                        scope_branch_id__isnull=False,
                    )
                    | models.Q(
                        scope_status__in=(
                            "organization",
                            "unresolved",
                        ),
                        scope_branch_id__isnull=True,
                        scope_department_id__isnull=True,
                    )
                ),
                name="audit_scope_status_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(scope_department_id__isnull=True) | models.Q(scope_branch_id__isnull=False)
                ),
                name="audit_scope_department_needs_branch",
            ),
            models.CheckConstraint(
                condition=models.Q(sensitivity__in=("standard", "compensation")),
                name="audit_sensitivity_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_attribution_status="exact",
                        actor_principal_kind__in=("user", "student", "teacher", "parent", "staff"),
                        actor_principal_id__isnull=False,
                    )
                    | models.Q(
                        actor_attribution_status="system",
                        actor__isnull=True,
                        actor_principal_kind="",
                        actor_principal_id__isnull=True,
                    )
                    | models.Q(
                        actor_attribution_status="unresolved",
                        actor_principal_kind="",
                        actor_principal_id__isnull=True,
                    )
                ),
                name="audit_actor_attribution_shape",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        target = f"{self.resource_type}#{self.resource_id}" if self.resource_type else "-"
        return f"{self.action} {target} by {self.actor_repr or 'system'}"
