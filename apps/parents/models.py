"""Parent / guardian domain models (TASKS §6).

`Guardian` is THE sanctioned parents→students link (a documented exception to
the no-cross-role-FK rule, per docs/adding-an-app.md routing note).
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.users.models import RoleAccount
from core.fields import EncryptedTextField
from core.historical_scope import (
    ATTRIBUTED_SCOPE_STATUSES,
    ScopeAttributionStatus,
    guard_immutable_scope_snapshot,
)


class ParentProfile(RoleAccount):
    class Gender(models.TextChoices):
        MALE = "m", _("Male")
        FEMALE = "f", _("Female")

    # Internal compatibility principal for permissions, sessions, and historical audit FKs.
    # It is provisioned automatically and is deliberately not editable or exposed as part
    # of the parent account. ParentProfile owns identity + login credentials.
    user = models.OneToOneField(
        "users.User", on_delete=models.PROTECT, related_name="parent_profile", editable=False
    )

    # --- Identity (owned by the parent, moving off users.User) ----------------
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    middle_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    birthdate = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=8, choices=Gender.choices, blank=True)
    workplace = models.CharField(max_length=200, blank=True)
    notes = EncryptedTextField(blank=True)

    # Immutable creation ownership for the interval before the first Guardian
    # link exists. Without it, any branch-scoped operator could claim an
    # unassigned profile by guessing its primary key. Historical rows whose
    # source cannot be proven stay unresolved and fail closed for scoped staff.
    branch_at_creation = models.ForeignKey(
        "org.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    department_at_creation = models.ForeignKey(
        "org.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )
    attribution_status = models.CharField(
        max_length=12,
        choices=ScopeAttributionStatus.choices,
        default=ScopeAttributionStatus.UNRESOLVED,
        db_default=ScopeAttributionStatus.UNRESOLVED,
        editable=False,
    )
    created_by = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="created_parent_profiles",
        editable=False,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("phone",),
                condition=~models.Q(phone=""),
                name="parent_phone_unique_nonblank",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="parent_email_unique_nonblank_ci",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        attribution_status__in=ATTRIBUTED_SCOPE_STATUSES,
                        branch_at_creation__isnull=False,
                    )
                    | models.Q(
                        attribution_status__in=(
                            ScopeAttributionStatus.UNRESOLVED,
                            ScopeAttributionStatus.CONFLICTING,
                            ScopeAttributionStatus.QUARANTINED,
                        ),
                        branch_at_creation__isnull=True,
                        department_at_creation__isnull=True,
                    )
                ),
                name="parent_creation_scope_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=(
                    "attribution_status",
                    "branch_at_creation",
                    "department_at_creation",
                ),
                name="parent_creation_scope_idx",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.get_full_name() or self.username or f"parent#{self.pk}"

    def get_full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    def clean(self) -> None:
        super().clean()
        if not self.phone and not self.email:
            raise ValidationError(
                {
                    "phone": [_("A phone or email is required.")],
                    "email": [_("A phone or email is required.")],
                }
            )

    def save(self, *args, **kwargs) -> None:
        guard_immutable_scope_snapshot(
            self,
            field_attnames=(
                "branch_at_creation_id",
                "department_at_creation_id",
                "attribution_status",
                "created_by_id",
            ),
            update_fields=kwargs.get("update_fields"),
        )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Parent identity history cannot be deleted; deactivate the account instead.")),
            {self},
        )


class Guardian(models.Model):
    class Relationship(models.TextChoices):
        MOTHER = "mother", _("Mother")
        FATHER = "father", _("Father")
        GRANDPARENT = "grandparent", _("Grandparent")
        LEGAL_GUARDIAN = "legal_guardian", _("Legal guardian")
        OTHER = "other", _("Other")

    parent = models.ForeignKey(ParentProfile, on_delete=models.PROTECT, related_name="guardianships")
    student = models.ForeignKey("students.StudentProfile", on_delete=models.PROTECT, related_name="guardians")
    relationship = models.CharField(max_length=16, choices=Relationship.choices)
    is_primary = models.BooleanField(default=False)
    custody_notes = EncryptedTextField(blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True, db_index=True, editable=False)
    revoked_by = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        ordering = ("student", "-is_primary")
        constraints = [
            models.UniqueConstraint(
                fields=("parent", "student"),
                condition=Q(revoked_at__isnull=True),
                name="one_active_guardian_link",
            ),
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(is_primary=True, revoked_at__isnull=True),
                name="one_primary_guardian_per_student",
            ),
        ]
        indexes = [
            models.Index(fields=("student", "revoked_at", "is_primary"), name="guardian_active_student_idx"),
            models.Index(fields=("parent", "revoked_at"), name="guardian_active_parent_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.parent_id}->{self.student_id}"

    def save(self, *args, **kwargs) -> None:
        if self.revoked_at is None and self.revoked_by_id is not None:
            raise ValidationError({"revoked_by": [_("A revocation timestamp is required.")]})
        if not self._state.adding and self.pk:
            previous = type(self).objects.filter(pk=self.pk).values("revoked_at", "revoked_by_id").first()
            if (
                previous is not None
                and previous["revoked_at"] is not None
                and (
                    previous["revoked_at"] != self.revoked_at
                    or previous["revoked_by_id"] != self.revoked_by_id
                )
            ):
                raise ValidationError({"revoked_at": [_("Guardian revocation history is immutable.")]})
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Guardian history cannot be deleted; revoke the relationship instead.")),
            {self},
        )


class PickupAuthorization(models.Model):
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.PROTECT, related_name="pickup_authorizations"
    )
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32)
    relationship = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deactivated_at = models.DateTimeField(null=True, blank=True, db_index=True, editable=False)
    deactivated_by = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        editable=False,
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("student", "is_active", "-created_at"), name="pickup_student_active_idx")
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}:{self.full_name}"

    def save(self, *args, **kwargs) -> None:
        if self.is_active and (self.deactivated_at is not None or self.deactivated_by_id is not None):
            raise ValidationError(
                {"is_active": [_("A deactivated pickup authorization cannot be reactivated.")]}
            )
        if not self.is_active and self.deactivated_at is None:
            raise ValidationError({"deactivated_at": [_("A deactivation timestamp is required.")]})
        if not self._state.adding and self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("student_id", "is_active", "deactivated_at", "deactivated_by_id")
                .first()
            )
            if previous is not None and previous["student_id"] != self.student_id:
                raise ValidationError(
                    {"student": [_("A pickup authorization cannot be moved to another student.")]}
                )
            if (
                previous is not None
                and not previous["is_active"]
                and (
                    self.is_active
                    or previous["deactivated_at"] != self.deactivated_at
                    or previous["deactivated_by_id"] != self.deactivated_by_id
                )
            ):
                raise ValidationError({"is_active": [_("Pickup deactivation history is immutable.")]})
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Pickup authorization history cannot be deleted; deactivate it instead.")),
            {self},
        )
