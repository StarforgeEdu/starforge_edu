"""Student domain models (TASKS sections 5-6)."""

from __future__ import annotations

from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.users.models import RoleAccount
from core.fields import EncryptedTextField


class StudentProfile(RoleAccount):
    class Status(models.TextChoices):
        LEAD = "lead", _("Lead")
        APPLICATION = "application", _("Application")
        ACCEPTED = "accepted", _("Accepted")
        ENROLLED = "enrolled", _("Enrolled")
        ACTIVE = "active", _("Active")
        GRADUATED = "graduated", _("Graduated")
        WITHDRAWN = "withdrawn", _("Withdrawn")

    class Gender(models.TextChoices):
        MALE = "m", _("Male")
        FEMALE = "f", _("Female")

    # Internal compatibility principal for permissions, sessions, and historical audit FKs.
    # It is provisioned automatically and is deliberately not editable or exposed as part
    # of the student account. StudentProfile owns identity + login credentials.
    user = models.OneToOneField(
        "users.User", on_delete=models.CASCADE, related_name="student_profile", editable=False
    )
    student_id = models.CharField(max_length=32, unique=True)

    # --- Identity (owned by the student, moving off users.User) ---------------
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    middle_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=32, blank=True, db_index=True)
    email = models.EmailField(blank=True)
    birthdate = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=8, choices=Gender.choices, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.LEAD, db_index=True)
    branch = models.ForeignKey("org.Branch", on_delete=models.PROTECT, related_name="students")
    current_cohort = models.ForeignKey(
        "cohorts.Cohort",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="current_students",
    )
    enrollment_date = models.DateField(null=True, blank=True)
    academic_level = models.CharField(max_length=64, blank=True)
    location = models.CharField(max_length=200, blank=True)  # F2-1: city/area for filtering
    previous_school = models.CharField(max_length=200, blank=True)  # F2-1: academic school at intake
    medical_notes = EncryptedTextField(blank=True)
    emergency_contacts = models.JSONField(default=list, blank=True)
    photo = models.ImageField(upload_to="students/photos/", blank=True)
    # F2-2: soft block — a barred-but-still-enrolled student (disciplinary/financial),
    # distinct from the WITHDRAWN terminal status. Null = not blocked.
    blocked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    block_reason = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("phone",),
                condition=~models.Q(phone=""),
                name="student_phone_unique_nonblank",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=~models.Q(email=""),
                name="student_email_unique_nonblank_ci",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "branch")),
            # Serve the default newest-first directory list (ORDER BY created_at DESC, id)
            # from an index instead of a full sort — StudentProfile is the hottest, most
            # unbounded list in the product (leads/withdrawn/graduated never deleted).
            models.Index(fields=("-created_at", "id"), name="student_created_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.student_id

    def get_full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    @property
    def is_blocked(self) -> bool:
        return self.blocked_at is not None


class EnrollmentReason(models.Model):
    """Per-Center configurable reason for an enrollment status change (why a lead
    dropped, why a student withdrew, …). Every center categorizes differently, so
    the reasons are data, not a hardcoded enum. Seeded per tenant with the defaults
    (completed/moved_city/financial/behavior/schedule_conflict/other). Mirrors
    schedule.LessonType / academics.ExamType.

    An EnrollmentEvent stores the reason as a denormalized ``reason_code`` SLUG (not
    an FK) so the historical log keeps the reason even if the center later renames or
    retires it — the config table is the source of *valid* reasons at write time."""

    name = models.CharField(max_length=64)
    slug = models.SlugField(max_length=64, unique=True)
    color = models.CharField(max_length=16, blank=True)  # optional UI hint, e.g. "#3b82f6"
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class EnrollmentEvent(models.Model):
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE, related_name="enrollment_events")
    from_status = models.CharField(max_length=16)
    to_status = models.CharField(max_length=16)
    # A slug validated at write time against the active EnrollmentReason rows (kept
    # denormalized so history survives a reason being renamed/retired). Width MUST
    # match EnrollmentReason.slug (64) so any active reason can actually be recorded.
    reason_code = models.CharField(max_length=64, blank=True)
    note = models.TextField(blank=True)
    actor = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.student_id}:{self.from_status}->{self.to_status}"


class StudentIdCounter(models.Model):
    """Per-year monotonic counter for generated student IDs (locked on use)."""

    year = models.PositiveSmallIntegerField(unique=True)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-year",)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.year}:{self.last_value}"
