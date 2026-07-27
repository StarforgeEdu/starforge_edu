"""Parent / guardian domain models (TASKS §6).

`Guardian` is THE sanctioned parents→students link (a documented exception to
the no-cross-role-FK rule, per docs/adding-an-app.md routing note).
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.users.models import RoleAccount


class ParentProfile(RoleAccount):
    class Gender(models.TextChoices):
        MALE = "m", _("Male")
        FEMALE = "f", _("Female")

    # Internal compatibility principal for permissions, sessions, and historical audit FKs.
    # It is provisioned automatically and is deliberately not editable or exposed as part
    # of the parent account. ParentProfile owns identity + login credentials.
    user = models.OneToOneField(
        "users.User", on_delete=models.CASCADE, related_name="parent_profile", editable=False
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
    notes = models.TextField(blank=True)

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
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.get_full_name() or self.username or f"parent#{self.pk}"

    def get_full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)


class Guardian(models.Model):
    class Relationship(models.TextChoices):
        MOTHER = "mother", _("Mother")
        FATHER = "father", _("Father")
        GRANDPARENT = "grandparent", _("Grandparent")
        LEGAL_GUARDIAN = "legal_guardian", _("Legal guardian")
        OTHER = "other", _("Other")

    parent = models.ForeignKey(ParentProfile, on_delete=models.CASCADE, related_name="guardianships")
    student = models.ForeignKey("students.StudentProfile", on_delete=models.CASCADE, related_name="guardians")
    relationship = models.CharField(max_length=16, choices=Relationship.choices)
    is_primary = models.BooleanField(default=False)
    custody_notes = models.TextField(blank=True)

    class Meta:
        unique_together = (("parent", "student"),)
        ordering = ("student", "-is_primary")
        constraints = [
            models.UniqueConstraint(
                fields=["student"],
                condition=Q(is_primary=True),
                name="one_primary_guardian_per_student",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.parent_id}->{self.student_id}"


class PickupAuthorization(models.Model):
    student = models.ForeignKey(
        "students.StudentProfile", on_delete=models.CASCADE, related_name="pickup_authorizations"
    )
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32)
    relationship = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}:{self.full_name}"
