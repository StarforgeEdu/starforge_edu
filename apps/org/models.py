"""Per-tenant organizational structure: Branch + Department.

Lives in tenant schemas only. A row's tenant is the schema it lives in;
no FK to Center is needed because django-tenants enforces isolation at
the connection level.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.users.models import RoleAccount
from core.validators import validate_iana_timezone


def _default_allowed_file_types() -> list[str]:
    return ["pdf", "mp4", "pptx", "docx", "mp3", "m4a", "webm", "jpg", "jpeg", "png", "webp"]  # D2-E-2


def _default_otp_channel_prefs() -> dict[str, bool]:
    return {"sms": True, "email": True}


class StaffProfile(RoleAccount):
    """A staff member (director / registrar / cashier / accountant / librarian / …) as a
    role-native account: it OWNS the person's identity + login credentials (via RoleAccount).
    Their specific roles + branch scope come from users.RoleMembership. Distinct from
    teachers (TeacherProfile) and platform admins (who stay plain Django Users for /admin/).
    Tenant-scoped — platform staff on the public schema get NO StaffProfile."""

    class Gender(models.TextChoices):
        MALE = "m", _("Male")
        FEMALE = "f", _("Female")

    # Internal compatibility principal for permissions, sessions, and historical audit FKs.
    # StaffProfile owns identity + login credentials; operators never select this relation.
    user = models.OneToOneField(
        "users.User", on_delete=models.CASCADE, related_name="staff_profile", editable=False
    )
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    middle_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    birthdate = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=8, choices=Gender.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("last_name", "first_name")
        constraints = [
            models.UniqueConstraint(
                fields=("phone",),
                condition=~models.Q(phone=""),
                name="staff_phone_unique_nonblank",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="staff_email_unique_nonblank_ci",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.get_full_name() or self.username or f"staff#{self.pk}"

    def get_full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)


class Branch(models.Model):
    """A physical location of the education center (city / building)."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    address = models.CharField(max_length=512, blank=True)
    phone = models.CharField(max_length=32, blank=True)

    timezone = models.CharField(
        max_length=64,
        default="Asia/Tashkent",
        validators=[validate_iana_timezone],
    )
    is_active = models.BooleanField(default=True)

    # Soft capacity caps (null = unlimited). These never block writes — they
    # surface a `capacity_status.over` flag for the UI (D1-LF-5).
    max_students = models.PositiveIntegerField(null=True, blank=True)
    max_teachers = models.PositiveIntegerField(null=True, blank=True)

    # Soft delete: `destroy` archives instead of deleting (D1-LF-7).
    archived_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)
        verbose_name_plural = "Branches"

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Branch history cannot be deleted; archive the branch instead.")),
            {self},
        )


class Department(models.Model):
    """A teaching/admin unit inside a Branch (math, languages, finance, etc.)."""

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="departments")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100)
    description = models.TextField(blank=True, validators=[MaxLengthValidator(4_000)])
    is_active = models.BooleanField(default=True)

    # Compatibility FK for the existing authorization graph. Admin/API expose a
    # TeacherProfile id and resolve this bridge internally; operators never choose User.
    head = models.ForeignKey(
        "users.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="headed_departments",
    )
    budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("branch", "slug"),)
        ordering = ("branch", "name")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(budget__isnull=True) | models.Q(budget__gte=0),
                name="department_budget_nonnegative",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch.name}/{self.name}"

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Department history cannot be deleted; deactivate the department instead.")),
            {self},
        )


class CenterSettings(models.Model):
    """Per-Center knob store (TD-13). Tenant-schema singleton at pk=1.

    Every school-variable number lives here instead of as a code constant.
    Consume through the cached accessor `apps.org.selectors.get_center_settings`,
    never by querying this table in a hot path.
    """

    class GradingScheme(models.TextChoices):
        LETTER = "letter", _("Letter (A–F)")
        GPA = "gpa", _("GPA (0–4)")
        PERCENTAGE = "percentage", _("Percentage (0–100)")

    class Language(models.TextChoices):
        UZBEK = "uz", _("Uzbek")
        RUSSIAN = "ru", _("Russian")
        ENGLISH = "en", _("English")

    open_registration = models.BooleanField(default=False)  # TD-17
    # F1-8 / D-8: when True, a reception group proposal needs a manager's acceptance
    # before the lead is enrolled; when False, reception's proposal enrolls directly.
    require_group_acceptance = models.BooleanField(default=False)
    # D4-LF-3 (TD-13): the center's default notification language. Blank means
    # "no preference" — the locale fallback chain then uses the en→uz lingua
    # franca order. A center serving Uzbek can set "uz" to prefer it over en.
    default_language = models.CharField(
        max_length=8,
        blank=True,
        default="",
        choices=Language.choices,
        help_text=_("Default notification language; blank uses the en→uz fallback."),
    )
    organization_timezone = models.CharField(
        max_length=64,
        default="Asia/Tashkent",
        validators=[validate_iana_timezone],
        help_text=_("IANA timezone used for organization-wide business dates."),
    )
    grading_scheme = models.CharField(
        max_length=16, choices=GradingScheme.choices, default=GradingScheme.PERCENTAGE
    )
    honor_roll_min = models.DecimalField(  # D2-C-2
        max_digits=5, decimal_places=2, default=Decimal("90")
    )
    academic_warning_max = models.DecimalField(  # D2-C-2
        max_digits=5, decimal_places=2, default=Decimal("60")
    )
    late_threshold_minutes = models.PositiveSmallIntegerField(default=10)
    attendance_correction_window_hours = models.PositiveSmallIntegerField(default=24)
    auto_absent_after_minutes = models.PositiveSmallIntegerField(default=30)  # D2-B-2
    assignment_grace_minutes = models.PositiveSmallIntegerField(default=0)
    assignment_max_resubmits = models.PositiveSmallIntegerField(default=2)  # D2-D-2
    max_upload_mb = models.PositiveIntegerField(default=200)  # D2-E uses this as max_file_size_mb
    storage_quota_gb = models.PositiveIntegerField(null=True, blank=True)  # D2-E-2 (null = unlimited)
    allowed_file_types = models.JSONField(default=_default_allowed_file_types)
    currency_primary = models.CharField(max_length=3, default="UZS")
    currency_secondary = models.CharField(max_length=3, default="USD")
    fx_source = models.CharField(max_length=32, default="cbu")
    # D3-A finance knobs (consumed by apps/finance/services.py):
    fx_rate_usd_manual = models.DecimalField(  # used when fx_source == "manual"
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    sibling_discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    payment_reminder_interval_days = models.PositiveSmallIntegerField(default=3)
    quiet_hours_start = models.TimeField(default=time(22, 0))
    quiet_hours_end = models.TimeField(default=time(7, 0))
    otp_channel_prefs = models.JSONField(default=_default_otp_channel_prefs)
    otp_cooldown_seconds = models.PositiveSmallIntegerField(default=60)
    student_id_pattern = models.CharField(max_length=64, default="{CODE}-{YYYY}-{NNNNN}")
    center_code = models.CharField(max_length=16, blank=True)
    # D4-LA-7 (TD-13): gates the request-driven AI exam-generation endpoint.
    ai_exam_generation_enabled = models.BooleanField(default=False)
    # F8-1: which placement question types this center allows when authoring tests.
    # Empty (default) = no restriction (all types). A non-empty list restricts both
    # manual and AI authoring to exactly those PlacementQuestion.QuestionType values.
    placement_allowed_question_types = models.JSONField(default=list, blank=True)
    # F24-1: when a student's total ACTIVE penalty points cross this threshold, the
    # crossing penalty is flagged + branch managers are notified. 0 = disabled.
    penalty_escalation_threshold = models.PositiveSmallIntegerField(default=0)
    # F15-1: when False, the student/parent report omits classroom rank entirely — some
    # centers reject ranking on principle (dignity DNA). Default True (rank shown).
    show_classroom_rank = models.BooleanField(default=True)
    # F23-1: when True, a manager may request an `absence_deduction` (an A-1 KIND) that
    # credits a student for a lesson they missed — dignity DNA (don't charge for teaching
    # not delivered). Off by default; a center opts in to the policy.
    absence_deduction_enabled = models.BooleanField(default=False)
    # F23-1: when True (default) only an EXCUSED absence (one with an accepted reason)
    # qualifies for a deduction; when False a plain ABSENT record qualifies too.
    absence_deduction_excused_only = models.BooleanField(default=True)
    # F8-2: when True, placement test AUTHORING (create test / add-remove question /
    # AI-generate) is restricted to the mobile app — a request without `X-Client: mobile`
    # is 403'd. A soft, spoofable policy gate (a determined web client can send the
    # header); the intent is to steer staff to the mobile authoring tools. Off by default.
    placement_test_creation_mobile_only = models.BooleanField(default=False)
    # Durable source of truth for tenant runtime feature isolation. Redis is a
    # performance cache only; a cache restart must not silently re-enable an
    # application that an operator disabled.
    disabled_apps = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Center settings"
        verbose_name_plural = "Center settings"
        constraints = [
            models.CheckConstraint(condition=models.Q(pk=1), name="center_settings_singleton_pk"),
            models.CheckConstraint(
                condition=models.Q(honor_roll_min__gte=0, honor_roll_min__lte=100),
                name="center_settings_honor_roll_range",
            ),
            models.CheckConstraint(
                condition=models.Q(academic_warning_max__gte=0, academic_warning_max__lte=100),
                name="center_settings_warning_range",
            ),
            models.CheckConstraint(
                condition=models.Q(academic_warning_max__lte=models.F("honor_roll_min")),
                name="center_settings_grade_threshold_order",
            ),
            models.CheckConstraint(
                condition=models.Q(sibling_discount_percent__gte=0, sibling_discount_percent__lte=100),
                name="center_settings_sibling_discount_range",
            ),
            models.CheckConstraint(
                condition=models.Q(fx_source__in=("cbu", "manual")),
                name="center_settings_fx_source_known",
            ),
            models.CheckConstraint(
                condition=models.Q(fx_rate_usd_manual__isnull=True) | models.Q(fx_rate_usd_manual__gt=0),
                name="center_settings_manual_fx_positive",
            ),
            models.CheckConstraint(
                condition=~models.Q(fx_source="manual") | models.Q(fx_rate_usd_manual__isnull=False),
                name="center_settings_manual_fx_present",
            ),
            models.CheckConstraint(
                condition=models.Q(currency_primary__regex=r"^[A-Z]{3}$"),
                name="center_settings_primary_currency_format",
            ),
            models.CheckConstraint(
                condition=models.Q(currency_secondary__regex=r"^[A-Z]{3}$"),
                name="center_settings_secondary_currency_format",
            ),
            models.CheckConstraint(
                condition=~models.Q(currency_primary=models.F("currency_secondary")),
                name="center_settings_currencies_distinct",
            ),
            models.CheckConstraint(
                condition=models.Func(
                    models.F("disabled_apps"),
                    function="org_disabled_apps_valid",
                    output_field=models.BooleanField(),
                ),
                name="center_settings_disabled_apps_valid",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return "CenterSettings"

    def clean(self) -> None:
        super().clean()
        errors: dict[str, list[str]] = {}
        if not 0 <= self.academic_warning_max <= self.honor_roll_min <= 100:
            errors["academic_warning_max"] = [
                str(_("Use a value from 0 to 100 that is not above honor_roll_min."))
            ]
            errors["honor_roll_min"] = [str(_("Use a value from 0 to 100."))]
        if not 0 <= self.sibling_discount_percent <= 100:
            errors["sibling_discount_percent"] = [str(_("Use a percentage from 0 to 100."))]
        if self.fx_source not in {"cbu", "manual"}:
            errors["fx_source"] = [str(_("Choose cbu or manual."))]
        if self.fx_rate_usd_manual is not None and self.fx_rate_usd_manual <= 0:
            errors["fx_rate_usd_manual"] = [str(_("Enter a positive exchange rate."))]
        if self.fx_source == "manual" and self.fx_rate_usd_manual is None:
            errors["fx_rate_usd_manual"] = [str(_("A manual exchange rate is required."))]
        for field_name in ("currency_primary", "currency_secondary"):
            value = getattr(self, field_name, "")
            if len(value) != 3 or not value.isascii() or not value.isalpha() or value != value.upper():
                errors[field_name] = [str(_("Enter an uppercase three-letter currency code."))]
        if self.currency_primary == self.currency_secondary:
            errors["currency_secondary"] = [str(_("Choose a different currency."))]
        from core.availability import APP_MOUNTS, PROTECTED_APPS

        valid_apps = set(APP_MOUNTS.values()) - PROTECTED_APPS
        disabled_shape_invalid = not isinstance(self.disabled_apps, list)
        if not disabled_shape_invalid:
            disabled_shape_invalid = any(
                not isinstance(app, str) or app not in valid_apps for app in self.disabled_apps
            )
        if not disabled_shape_invalid:
            disabled_shape_invalid = len(self.disabled_apps) != len(set(self.disabled_apps))
        if disabled_shape_invalid:
            errors["disabled_apps"] = [str(_("Choose unique, supported application labels."))]
        if errors:
            raise ValidationError(errors)

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Organization settings cannot be deleted.")),
            {self},
        )

    @classmethod
    def load(cls) -> CenterSettings:
        """Provision or retrieve the singleton from an explicit write path.

        Read paths use ``get_center_settings`` and never call this creator.
        """
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class Room(models.Model):
    """A bookable space inside a Branch."""

    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="rooms")
    name = models.CharField(max_length=100)
    capacity = models.PositiveSmallIntegerField(default=0)
    equipment = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, validators=[MaxLengthValidator(4_000)])

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = (("branch", "name"),)
        ordering = ("branch", "name")

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch_id}:{self.name}"

    def delete(self, *args, **kwargs):
        raise ProtectedError(
            str(_("Room history cannot be deleted; deactivate the room instead.")),
            {self},
        )

    def clean(self) -> None:
        super().clean()
        if not isinstance(self.equipment, list):
            raise ValidationError({"equipment": [_("Must be a list of equipment names.")]})
        if len(self.equipment) > 64:
            raise ValidationError({"equipment": [_("At most 64 items are allowed.")]})
        normalized: list[str] = []
        for item in self.equipment:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 100:
                raise ValidationError(
                    {"equipment": [_("Each item must be a non-empty string of at most 100 characters.")]}
                )
            normalized.append(item.strip())
        if len(normalized) != len(set(normalized)):
            raise ValidationError({"equipment": [_("Duplicate items are not allowed.")]})
        self.equipment = normalized


class BranchWorkingHours(models.Model):
    """One row per (branch, weekday). Replaced wholesale via the bulk-set
    endpoint (D1-LF-2)."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, _("Monday")
        TUESDAY = 1, _("Tuesday")
        WEDNESDAY = 2, _("Wednesday")
        THURSDAY = 3, _("Thursday")
        FRIDAY = 4, _("Friday")
        SATURDAY = 5, _("Saturday")
        SUNDAY = 6, _("Sunday")

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="working_hours")
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    opens_at = models.TimeField()
    closes_at = models.TimeField()
    is_closed = models.BooleanField(default=False)

    class Meta:
        unique_together = (("branch", "weekday"),)
        ordering = ("branch", "weekday")
        constraints = [
            models.CheckConstraint(
                condition=models.Q(is_closed=True) | models.Q(opens_at__lt=models.F("closes_at")),
                name="working_hours_open_before_close_or_closed",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch_id}:{self.weekday}"


class BranchHoliday(models.Model):
    """A per-branch closed/special day, layered over national holidays (D2-A)."""

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="holidays")
    date = models.DateField()
    name = models.CharField(max_length=200)
    is_working_day_override = models.BooleanField(default=False)

    class Meta:
        unique_together = (("branch", "date"),)
        ordering = ("date",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.branch_id}:{self.date}"


class BranchTransfer(models.Model):
    """Append-only student branch movement with immutable public attribution."""

    class AttributionStatus(models.TextChoices):
        RESOLVED = "resolved", _("Resolved")
        UNRESOLVED = "unresolved", _("Unresolved")

    # Retained only as an internal compatibility principal for old relations;
    # API presenters must use the role-native student fields below.
    user = models.ForeignKey("users.User", on_delete=models.PROTECT, related_name="branch_transfers")
    student = models.ForeignKey(
        "students.StudentProfile",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="branch_transfers",
        editable=False,
    )
    student_public_id = models.CharField(max_length=32, blank=True, editable=False)
    student_name = models.CharField(max_length=452, blank=True, editable=False)
    student_attribution_status = models.CharField(
        max_length=12,
        choices=AttributionStatus.choices,
        default=AttributionStatus.UNRESOLVED,
        editable=False,
    )
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="transfers_out")
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="transfers_in")
    reason = models.CharField(max_length=64, blank=True)
    actor = models.ForeignKey(
        "users.User",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="transfers_made",
    )
    actor_principal_kind = models.CharField(max_length=16, blank=True, editable=False)
    actor_principal_id = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    actor_name = models.CharField(max_length=452, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        student_attribution_status="resolved",
                        student__isnull=False,
                    )
                    & ~models.Q(student_public_id="")
                )
                | models.Q(
                    student_attribution_status="unresolved",
                    student__isnull=True,
                    student_public_id="",
                    student_name="",
                ),
                name="branch_transfer_student_attribution_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        actor_principal_id__isnull=True,
                        actor_principal_kind="",
                        actor_name="",
                    )
                    | models.Q(
                        actor__isnull=False,
                        actor_principal_id__isnull=False,
                        actor_principal_kind__in=("staff", "teacher", "student", "parent"),
                    )
                ),
                name="branch_transfer_actor_principal_consistent",
            ),
            models.CheckConstraint(
                condition=~models.Q(from_branch=models.F("to_branch")),
                name="branch_transfer_branches_differ",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.user_id}:{self.from_branch_id}->{self.to_branch_id}"
