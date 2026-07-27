"""Teacher domain models (TASKS §7)."""

from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.users.models import RoleAccount


class TeacherProfile(RoleAccount):
    class SalaryType(models.TextChoices):
        HOURLY = "hourly", _("Hourly")
        MONTHLY = "monthly", _("Monthly")

    class Gender(models.TextChoices):
        MALE = "m", _("Male")
        FEMALE = "f", _("Female")

    # Internal compatibility principal for permissions, sessions, and historical audit FKs.
    # It is provisioned automatically and is deliberately not editable or exposed as part
    # of the teacher account. TeacherProfile owns identity + login credentials.
    user = models.OneToOneField(
        "users.User", on_delete=models.CASCADE, related_name="teacher_profile", editable=False
    )

    # --- Identity (owned by the teacher, moving off users.User) ---------------
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    middle_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    birthdate = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=8, choices=Gender.choices, blank=True)
    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="teachers")
    department = models.ForeignKey(
        "org.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teachers",
    )
    hire_date = models.DateField(null=True, blank=True)
    subjects = models.JSONField(default=list, blank=True)
    qualifications = models.TextField(blank=True)
    salary_type = models.CharField(max_length=8, choices=SalaryType.choices, default=SalaryType.MONTHLY)
    rate = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    is_substitute = models.BooleanField(default=False, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("phone",),
                condition=~models.Q(phone=""),
                name="teacher_phone_unique_nonblank",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="teacher_email_unique_nonblank_ci",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.get_full_name() or self.username or f"teacher#{self.pk}"

    def get_full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)


class PayoutPolicy(models.Model):
    """F13-1 — a teacher's DYNAMIC pay rule, configured per teacher via the API. Every
    education centre pays differently (hourly, a % of the tuition their students actually
    pay, a flat monthly wage), so the METHOD + its parameters are data, not code. A
    salary-prep run COMPUTES the amount from this policy for a period, then routes it
    through the A-1 approvals engine (kind=salary_prep) for a manager to approve and a
    cashier to disburse — the teacher never sets or pays their own salary. One policy per
    teacher (their current active rule)."""

    class Method(models.TextChoices):
        HOURLY = "hourly", _("Per taught hour")
        PERCENT_OF_TUITION = "percent_of_collected_tuition", _("% of collected tuition")
        FLAT_MONTHLY = "flat_monthly", _("Flat amount per period")

    teacher = models.OneToOneField(TeacherProfile, on_delete=models.CASCADE, related_name="payout_policy")
    method = models.CharField(max_length=32, choices=Method.choices)
    # Per taught hour (HOURLY).
    hourly_rate_uzs = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # Flat amount for the whole period (FLAT_MONTHLY).
    flat_amount_uzs = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # 0-100 % of tuition collected from the teacher's students (PERCENT_OF_TUITION).
    tuition_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"payout#{self.teacher_id}:{self.method}"
