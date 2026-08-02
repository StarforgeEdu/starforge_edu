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

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _


class AccountType(models.Model):
    """A tenant-defined account type whose grants are enforced live.

    ``is_system`` rows mirror the legacy :class:`core.permissions.Role` values
    and are seeded by the data migration. Custom rows let a centre compose a
    staff/teacher/student/parent account type without changing application code.
    """

    class AccountKind(models.TextChoices):
        STAFF = "staff", _("Staff")
        TEACHER = "teacher", _("Teacher")
        STUDENT = "student", _("Student")
        PARENT = "parent", _("Parent")

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    account_kind = models.CharField(max_length=16, choices=AccountKind.choices, db_index=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    is_system = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("account_kind", "name", "pk")

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    @property
    def is_owner_type(self) -> bool:
        """The one protected system type allowed to hold security wildcards."""
        return self.is_system and self.slug == "director"

    @property
    def compatibility_role(self) -> str:
        """Legacy role stored on RoleMembership for row-scope compatibility."""
        from core.permissions import Role

        if self.is_system and self.slug in Role.ALL:
            return self.slug
        compatibility_roles: dict[str, str] = {
            self.AccountKind.TEACHER: Role.TEACHER,
            self.AccountKind.STUDENT: Role.STUDENT,
            self.AccountKind.PARENT: Role.PARENT,
        }
        return compatibility_roles.get(self.account_kind, Role.SUPPORT)


class AccountTypePermission(models.Model):
    """One validated ``resource:verb`` grant attached to an account type."""

    account_type = models.ForeignKey(
        AccountType,
        on_delete=models.CASCADE,
        related_name="permission_rows",
    )
    permission = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("account_type_id", "permission")
        constraints = [
            models.UniqueConstraint(
                fields=("account_type", "permission"),
                name="one_grant_per_account_type_permission",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.account_type.slug}:{self.permission}"

    def save(self, *args, **kwargs) -> None:
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if not self.account_type_id:
            return
        from apps.access.validation import validate_account_type_permission
        from core.exceptions import ValidationException

        try:
            self.permission = validate_account_type_permission(
                self.permission,
                account_type=self.account_type,
            )
        except ValidationException as exc:
            # Model/admin writes should surface Django-native validation while
            # the HTTP service keeps the project's structured domain error.
            raise ValidationError({"permission": [str(exc.detail)]}) from exc


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
            models.CheckConstraint(
                condition=~models.Q(permission__startswith="access:"),
                name="no_access_resource_override",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.role}:{self.effect}:{self.permission}"
